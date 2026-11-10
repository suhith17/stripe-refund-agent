from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from payresolve import create_app
from payresolve.catalog import CatalogStore, execute, last_week, seed_catalog
from payresolve.config import Settings
from payresolve.stripe_gateway import SandboxError
from payresolve.workflow import SandboxWorkflow
from tests.test_workflow import FakeStripe


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'catalog.db'
        self.store = CatalogStore(self.path)
        self.gateway = FakeStripe()

    def seed(self):
        return seed_catalog(self.store, self.gateway, 'demo')

    def test_seed_reopen_preserves_existing_data_and_refunded_balance(self):
        request = self.store.create('Existing operator request')
        old = SandboxWorkflow(self.store, self.gateway).create_payment('old-payment', 900)
        first = self.seed()
        friday = self.store.order('demo:a', 'ORD-102')
        SandboxWorkflow(self.store, self.gateway).refund('demo-refund', friday['payment_id'], 1000)
        self.store = CatalogStore(self.path)
        second = self.seed()
        self.assertEqual(first, second)
        self.assertEqual(len(self.gateway.payments), 4)
        self.assertEqual(self.store.get(request['id']), request)
        self.assertEqual(self.gateway.payments[friday['payment_id']]['refundable_amount'], 4999)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        with self.assertRaises(SandboxError):
            self.store.order('demo:a', old['payment']['id'])

    def test_search_scopes_before_matching_and_uses_fixed_dates(self):
        self.seed()
        start, end = last_week()
        self.assertEqual((start, end), ('2026-10-26', '2026-11-01'))
        results = self.store.search('demo:a', 'headphones', purchased_from=start, purchased_to=end)
        self.assertEqual([r['order_id'] for r in results], ['ORD-101', 'ORD-102'])
        self.assertEqual(len(self.store.search('demo:b', 'headphones')), 1)
        self.assertEqual(self.store.search('demo:a', 'headphones ORD-201'), [])
        self.assertEqual(self.store.search('demo:a', 'unknown'), [])
        self.assertEqual(len(self.store.search('demo:a', 'headphones', purchased_from='2026-10-30')), 1)
        self.assertEqual(len(self.store.search('demo:a', 'ORD-102')), 1)
        with self.assertRaises(SandboxError):
            self.store.search('demo:a', purchased_from='invalid')

    def test_unauthorized_references_do_not_contact_provider(self):
        self.seed()
        hidden = self.store.order('demo:b', 'ORD-201')
        gateway = Mock()
        for reference in ('ORD-201', hidden['payment_id'], 'ORD-999'):
            with self.assertRaises(SandboxError) as caught:
                self.store.inspect_payment('demo:a', reference, gateway)
            self.assertEqual(caught.exception.code, 'order_not_found')
            self.assertEqual(caught.exception.status, 404)
        gateway.get_payment.assert_not_called()
        visible = self.store.inspect_payment('demo:a', 'ORD-102', self.gateway)
        self.assertEqual(visible['payment']['amount'], 5999)
        self.gateway.payments[visible['payment']['id']]['operation_key'] = 'wrong-owner'
        with self.assertRaises(SandboxError) as caught:
            self.store.inspect_payment('demo:a', 'ORD-102', self.gateway)
        self.assertEqual(caught.exception.code, 'payment_identity_mismatch')

    def test_interrupted_seed_reuses_created_payments(self):
        original = self.gateway.create_payment
        attempts = 0
        def failing(params, key):
            nonlocal attempts
            attempts += 1
            result = original(params, key)
            if attempts == 2:
                raise SandboxError('stripe_connection_failed', 'Lost payment response')
            return result
        self.gateway.create_payment = failing
        with self.assertRaises(SandboxError):
            self.seed()
        self.gateway.create_payment = original
        self.store = CatalogStore(self.path)
        self.seed()
        self.assertEqual(len(self.gateway.payments), 3)
        self.assertEqual(len(self.store.search('demo:a')), 2)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_namespace_isolation_and_invalid_seed(self):
        with self.assertRaises(SandboxError):
            seed_catalog(self.store, self.gateway, '../bad')
        self.assertEqual(self.gateway.payments, {})
        self.seed()
        seed_catalog(self.store, self.gateway, 'another')
        self.assertEqual(len(self.gateway.payments), 6)
        other = self.store.order('another:a', 'ORD-102')
        with self.assertRaises(SandboxError):
            self.store.order('demo:a', other['payment_id'])

    def test_web_selector_uses_session_scope_and_get_is_read_only(self):
        self.seed()
        app = create_app(Settings(self.path))
        client = app.test_client()
        self.assertEqual(client.get('/demo/orders/ORD-102').status_code, 403)
        client.get('/demo')
        with client.session_transaction() as session:
            token = session['csrf']
        self.assertEqual(client.post('/demo/customer', data={'customer_id':'demo:a'}).status_code, 403)
        self.assertEqual(client.post('/demo/customer', data={'csrf':token, 'customer_id':'demo:a'},
                                     headers={'Origin':'http://localhost'}).status_code, 303)
        page = client.get('/demo?q=headphones&customer_id=demo:b')
        self.assertIn(b'ORD-101', page.data)
        self.assertIn(b'ORD-102', page.data)
        self.assertNotIn(b'ORD-201', page.data)
        self.assertEqual(client.get('/demo/orders/ORD-201').status_code, 404)
        hidden = self.store.order('demo:b', 'ORD-201')
        self.assertEqual(client.get('/demo/orders/' + hidden['payment_id']).status_code, 404)
        self.assertEqual(client.get('/demo/orders/ORD-102').status_code, 200)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_catalog_cli_reads_without_creating_provider_client(self):
        self.seed()
        args = SimpleNamespace(demo_command='search', customer='demo:a', query='headphones', last_week=True)
        with patch('payresolve.catalog.StripeSandbox', side_effect=AssertionError('local read')):
            result = execute(args, Settings(self.path))
        self.assertEqual(len(result['orders']), 2)
        self.assertEqual(result['source'], 'local_catalog')

    def test_seed_refuses_to_overwrite_existing_ownership(self):
        self.seed()
        with self.store.connect() as connection:
            connection.execute("UPDATE customer_orders SET customer_id='demo:b' WHERE order_id='ORD-102'")
        with self.assertRaises(SandboxError) as caught:
            self.seed()
        self.assertEqual(caught.exception.code, 'demo_order_conflict')
        self.assertEqual(self.store.order('demo:b', 'ORD-102')['customer_id'], 'demo:b')


if __name__ == '__main__':
    unittest.main()
