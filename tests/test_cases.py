from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from payresolve import create_app
from payresolve.cases import CaseStore, CaseService
from payresolve.catalog import seed_catalog
from payresolve.config import Settings
from payresolve.execution import ExecutionStore, ReviewedExecution
from payresolve.gemini_gateway import ExtractionError
from payresolve.stripe_gateway import SandboxError
from payresolve.workflow import SandboxWorkflow, REPLAY_WINDOW_SECONDS
from tests.test_workflow import FakeStripe


class CaseTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'cases.db'
        self.store = CaseStore(self.path)
        self.gateway = FakeStripe()
        seed_catalog(self.store.catalog, self.gateway, 'test')
        self.service = CaseService(self.store, self.gateway)
        self.customer = 'test:a'
        self.case = self.store.create_case(self.customer, 'Refund my headphones')

    def proposal(self, case=None, amount=1000, order='ORD-102'):
        case = case or self.case
        return self.service.propose(self.customer, case['id'], order, amount, 'usd', case['version'])

    def confirm(self, case):
        return self.service.confirm(self.customer, case['id'], case['revision'], case['version'])

    def test_clarification_and_proposal_survive_reopen(self):
        case = self.store.clarify(self.customer, self.case['id'], ['ORD-101','ORD-102'], 'Which purchase?', self.case['version'])
        case = self.store.message(self.customer, case['id'], 'Friday, just $10.', case['version'])
        case = self.proposal(case)
        reopened = CaseStore(self.path).snapshot(self.customer, case['id'])
        self.assertEqual(reopened, case)
        self.assertEqual(case['selected_order'], 'ORD-102')
        self.assertEqual(case['amount_minor'], 1000)
        self.assertEqual(case['candidates'], ['ORD-101','ORD-102'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_confirm_committed_before_post_and_repeat_after_restart(self):
        case = self.proposal()
        def inspect_claim(key):
            saved = self.store.snapshot(self.customer, case['id'])
            self.assertEqual(saved['confirmed_revision'], 1)
            self.assertIsNotNone(saved['operation_id'])
        self.gateway.before_post = inspect_claim
        result = self.confirm(case)
        self.assertEqual(result['stage'], 'verified')
        restarted = CaseService(CaseStore(self.path), self.gateway)
        repeated = restarted.confirm(self.customer, case['id'], case['revision'], case['version'])
        self.assertEqual(repeated['refund_id'], result['refund_id'])
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_correction_invalidates_card_and_revised_amount_executes(self):
        old = self.proposal()
        corrected = self.store.message(self.customer, old['id'], 'Actually Monday, only $3.', old['version'])
        with self.assertRaises(SandboxError):
            self.confirm(old)
        current = self.proposal(corrected, amount=300, order='ORD-101')
        self.assertEqual(current['revision'], 2)
        with self.assertRaises(SandboxError):
            self.confirm(old)
        self.confirm(current)
        params = self.gateway.refund_posts[0][0]
        self.assertEqual(params['amount'], 300)
        self.assertEqual(params['payment_intent'], self.store.catalog.order(self.customer, 'ORD-101')['payment_id'])
        self.assertEqual(len(current['proposals']), 2)

    def test_cancel_blocks_confirmation_and_legacy_bypass(self):
        case = self.proposal()
        self.store.cancel(self.customer, case['id'], case['version'])
        with self.assertRaises(SandboxError):
            self.confirm(case)
        proposal = case['proposals'][-1]
        with self.assertRaises(SandboxError):
            ExecutionStore(self.path).approve(proposal['analysis_id'], proposal['payment_id'], 1000, 'usd')
        with self.assertRaises(SandboxError):
            SandboxWorkflow(self.store, self.gateway).refund('bypass', proposal['payment_id'], 1000)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_unknown_response_recovery_and_expiry_preserve_identity(self):
        case = self.proposal()
        self.gateway.lose_response = True
        unknown = self.confirm(case)
        self.assertEqual(unknown['stage'], 'needs_reconciliation')
        with self.assertRaises(SandboxError):
            self.store.message(self.customer, case['id'], 'Change amount', unknown['version'])
        recovered = CaseService(CaseStore(self.path), self.gateway).resume(self.customer, case['id'])
        self.assertEqual(recovered['stage'], 'verified')
        self.assertEqual(len(self.gateway.refunds), 1)
        self.assertEqual(self.gateway.refund_posts[0][1], self.gateway.refund_posts[1][1])

    def test_expired_unknown_outcome_refuses_new_submission(self):
        case = self.proposal()
        self.gateway.lose_response = True
        result = self.confirm(case)
        with self.store.connect() as c:
            c.execute('UPDATE stripe_operations SET created_at=? WHERE operation_id=?',
                      (time.time()-REPLAY_WINDOW_SECONDS-1, result['operation_id']))
        observed = self.service.resume(self.customer, case['id'])
        self.assertEqual(observed['stage'], 'needs_reconciliation')
        self.assertEqual(observed['last_error'], 'reconciliation_required')
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_claim_restart_and_concurrent_duplicate_confirmations(self):
        case = self.proposal()
        operation = self.store.claim(self.customer, case['id'], case['revision'], case['version'])
        self.assertEqual(self.gateway.refund_posts, [])
        service = CaseService(CaseStore(self.path), self.gateway)
        def confirm(_):
            return service.confirm(self.customer, case['id'], case['revision'], case['version'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, range(2)))
        self.assertTrue(all(r['stage'] == 'verified' for r in results))
        self.assertTrue(all(r['operation_id'] == operation for r in results))
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_cancel_racing_confirmation_has_one_winner(self):
        case = self.proposal()
        def action(which):
            try:
                if which == 'cancel':
                    self.store.cancel(self.customer, case['id'], case['version'])
                else:
                    self.store.claim(self.customer, case['id'], case['revision'], case['version'])
                return 'ok'
            except SandboxError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(action, ['cancel','confirm']))
        self.assertCountEqual(results, ['ok','conflict'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_other_customer_and_legacy_reads_and_writes_are_denied(self):
        with self.assertRaises(SandboxError):
            self.store.snapshot('test:b', self.case['id'])
        self.assertEqual(self.store.list_cases('test:b'), [])
        gateway = Mock()
        with self.assertRaises(SandboxError):
            CaseService(self.store, gateway).propose('test:b', self.case['id'], 'ORD-201', 100, 'usd', 1)
        gateway.get_payment.assert_not_called()
        case = self.proposal()
        proposal = case['proposals'][-1]
        for read in (lambda:self.store.get(case['request_id']), lambda:self.store.analysis(proposal['analysis_id'])):
            with self.assertRaises(SandboxError):
                read()
        operation = self.store.claim(self.customer, case['id'], case['revision'], case['version'])
        with self.assertRaises(SandboxError):
            ReviewedExecution(self.store, self.gateway).execute(proposal['analysis_id'])
        with self.assertRaises(SandboxError):
            SandboxWorkflow(self.store, self.gateway, case_customer_id=self.customer).refund(operation, proposal['payment_id'], 1200)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_fresh_eligibility_and_pending_status(self):
        case = self.proposal()
        payment = self.gateway.payments[case['proposals'][-1]['payment_id']]
        payment['disputed'] = True
        failed = self.confirm(case)
        self.assertEqual(failed['stage'], 'needs_reconciliation')
        self.assertEqual(self.gateway.refund_posts, [])
        payment['disputed'] = False
        self.gateway.refund_status = 'pending'
        result = self.service.resume(self.customer, case['id'])
        self.assertEqual(result['stage'], 'pending')
        self.service.resume(self.customer, case['id'])
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_stale_proposal_result_after_concurrent_correction_is_rejected(self):
        original = self.gateway.get_payment
        def read(payment_id):
            self.store.message(self.customer, self.case['id'], 'Wait, change the amount', self.case['version'])
            return original(payment_id)
        self.gateway.get_payment = read
        with self.assertRaises(SandboxError) as caught:
            self.proposal()
        self.assertEqual(caught.exception.code, 'stale_case')
        self.assertEqual(self.store.snapshot(self.customer, self.case['id'])['proposals'], [])

    def test_web_confirm_automatically_executes_and_scope_is_enforced(self):
        app = create_app(Settings(self.path))
        client = app.test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id'] = self.customer
            csrf = session['csrf']
        case = self.proposal()
        path = f"/demo/cases/{case['id']}"
        self.assertEqual(client.get(path).status_code, 200)
        with patch('payresolve.dashboard.StripeSandbox', return_value=self.gateway):
            data = {'csrf':csrf, 'version':str(case['version']), 'revision':str(case['revision'])}
            self.assertEqual(client.post(path+'/confirm',data=data).status_code, 400)
            response = client.post(path+'/confirm',data={**data,'confirm':'yes'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        self.assertIn(b'verified', client.get(path).data)
        self.assertEqual(client.get('/requests/'+case['request_id']).status_code, 403)
        with client.session_transaction() as session:
            session['demo_customer_id'] = 'test:b'
        self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.post(path+'/resume',data={'csrf':csrf,'version':1}).status_code, 404)

    def test_credentials_and_invalid_amounts_never_create_proposal(self):
        with self.assertRaises(ExtractionError):
            self.store.message(self.customer, self.case['id'], 'sk_test_secret', self.case['version'])
        for amount in (True, 0, -100, 90000):
            with self.assertRaises(SandboxError):
                self.proposal(amount=amount)
        self.assertEqual(self.store.snapshot(self.customer, self.case['id'])['proposals'], [])
        self.assertEqual(self.gateway.refund_posts, [])


if __name__ == '__main__':
    unittest.main()
