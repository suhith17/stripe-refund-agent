from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from payresolve import create_app
from payresolve.catalog import seed_catalog, add_demo_customer, FIXTURES
from payresolve.cases import CaseStore, CaseService
from payresolve.config import Settings
from payresolve.stripe_gateway import SandboxError
from tests.test_workflow import FakeStripe


class CustomerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'workspace.db'
        self.store = CaseStore(self.path)
        self.gateway = FakeStripe()
        seed_catalog(self.store.catalog, self.gateway, 'workspace')

    def client(self, customer='workspace:a'):
        client = create_app(Settings(self.path)).test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id'] = customer
        return client

    def test_extra_customer_is_idempotent_scoped_and_preserves_existing_balances(self):
        original = self.store.catalog.order('workspace:a', 'ORD-101')
        self.gateway.payments[original['payment_id']].update(amount_refunded=3999, refundable_amount=0)
        first = add_demo_customer(self.store.catalog, self.gateway, 'workspace')
        second = add_demo_customer(self.store.catalog, self.gateway, 'workspace')
        self.assertEqual(first['orders'], second['orders'])
        self.assertEqual(len(self.gateway.payments), 5)
        self.assertEqual(len(FIXTURES), 3)
        self.assertEqual(self.gateway.payments[original['payment_id']]['refundable_amount'], 0)
        with self.assertRaises(SandboxError):
            self.store.catalog.order('workspace:a', 'ORD-301')
        seed_catalog(self.store.catalog, self.gateway, 'evaluation')
        self.assertEqual(len([c for c in self.store.catalog.customers() if c['namespace'] == 'evaluation']), 2)
        self.assertEqual(len(self.store.catalog.search('evaluation:a')), 2)

    def test_purchase_balance_uses_recorded_observation_and_updates_after_refund(self):
        case = self.store.create_case('workspace:a', 'Refund $10 from ORD-101')
        service = CaseService(self.store, self.gateway)
        case = service.propose('workspace:a', case['id'], 'ORD-101', 1000, 'usd', case['version'])
        client = self.client()
        page = client.get('/demo/cases/' + case['id'])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'SELECTED PURCHASE', page.data)
        self.assertIn(b'Last checked', page.data)
        self.assertIn(b'id="refund-review"', page.data)
        case = service.confirm('workspace:a', case['id'], case['revision'], case['version'])
        self.gateway.get_payment = Mock(side_effect=AssertionError('Rendering must not contact Stripe'))
        page = client.get('/demo/cases/' + case['id'])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'$29.99', page.data)
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_case_names_are_scoped_and_new_messages_are_escaped(self):
        self.store.create_case('workspace:b', 'Private B purchase question')
        self.store.create_case('workspace:a', '<script>alert(1)</script> headphones')
        page = self.client().get('/demo')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'Private B purchase question', page.data)
        self.assertNotIn(b'<script>alert(1)</script>', page.data)
        self.assertIn(b'&lt;script&gt;', page.data)
