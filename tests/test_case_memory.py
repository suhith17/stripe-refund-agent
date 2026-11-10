from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from payresolve import create_app
from payresolve.agent import AgentRunner, build_context
from payresolve.cases import CaseStore
from payresolve.catalog import seed_catalog
from payresolve.config import Settings
from tests.test_agent import ScriptedPlanner, proposal_action
from tests.test_workflow import FakeStripe


class CaseMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'memory.db'
        self.store = CaseStore(self.path)
        self.gateway = FakeStripe()
        seed_catalog(self.store.catalog, self.gateway, 'memory')
        self.customer = 'memory:a'
        self.case = self.store.create_case(self.customer, 'Refund 10 from ORD-102.')

    def run_action(self, case, action):
        return AgentRunner(self.store, self.gateway, ScriptedPlanner([action])).run(self.customer, case['id'])

    def remember_request(self, case=None):
        case = case or self.case
        mid = case['messages'][0]['id']
        return self.run_action(case, dict(action='ask', reason='currency', order_id='ORD-102',
                order_message_id=mid, order_evidence='ORD-102', refund_type='partial', amount='10',
                amount_message_id=mid, amount_evidence='10'))

    def reply(self, case, text):
        return self.store.message(self.customer, case['id'], text, case['version'])

    def test_partial_details_survive_restart_and_recent_window_then_fill_currency(self):
        case = self.remember_request()
        original = case['messages'][0]['id']
        self.assertEqual(set(case['remembered_details']), {'order', 'amount'})
        for index in range(10):
            case = self.reply(case, f'Additional note: the packaging has scratch mark {chr(65 + index)}.')
        self.store = CaseStore(self.path)
        case = self.reply(self.store.snapshot(self.customer, case['id']), 'USD')
        context = build_context(self.store, self.customer, case, 'structured', [], 5)
        self.assertIn(original, [m['id'] for m in context['messages']])
        self.assertEqual(context['memory']['partial_details']['amount']['amount_minor'], 1000)
        history = build_context(self.store, self.customer, case, 'history', [], 5)
        self.assertNotIn('memory', history)
        case = self.run_action(case, lambda c: proposal_action(c, order_quote='ORD-102', amount_quote='10'))
        self.assertEqual(case['stage'], 'awaiting_confirmation')
        self.assertEqual(case['amount_minor'], 1000)
        self.assertIsNone(case['operation_id'])
        self.assertEqual(self.gateway.refund_posts, [])
        self.assertTrue(any(e['kind'] == 'case_memory_saved' for e in case['events']))

    def test_changed_amount_clears_only_amount_and_blocks_reviving_old_source(self):
        case = self.remember_request()
        case = self.reply(case, 'Make it 5 instead.')
        self.assertEqual(set(case['remembered_details']), {'order'})
        old = self.case['messages'][0]['id']
        case = self.run_action(case, dict(action='ask', reason='currency', refund_type='partial', amount='10',
                                         amount_message_id=old, amount_evidence='10'))
        self.assertNotIn('amount', case['remembered_details'])
        self.assertEqual(case['agent_turns'][-1]['status'], 'completed')
        self.assertTrue(any(e['kind'] == 'case_memory_rejected' and
                            e['details']['error'] == 'stale_amount_evidence' for e in case['events']))

    def test_repeated_explicit_order_refreshes_old_citation_without_reasking(self):
        case = self.remember_request()
        case = self.reply(case, 'Still ORD-102 please.')
        latest = case['messages'][-1]['id']
        case = self.reply(case, 'USD')
        old = self.case['messages'][0]['id']
        def stale_proposal(context):
            action = proposal_action(context, order_quote='ORD-102', amount_quote='10')
            action['order_message_id'] = old
            action['amount_message_id'] = old
            return action
        case = self.run_action(case, stale_proposal)
        self.assertEqual(case['stage'], 'awaiting_confirmation')
        self.assertEqual(case['remembered_details']['order']['message_id'], latest)
        self.assertEqual(case['amount_minor'], 1000)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_order_refresh_does_not_override_changed_or_ambiguous_selection(self):
        for text in ('Actually ORD-101 instead.', 'ORD-102 or ORD-101, not sure.'):
            with self.subTest(text=text):
                case = self.store.create_case(self.customer, 'Refund 10 from ORD-102.')
                old = case['messages'][0]['id']
                case = self.reply(case, text)
                case = self.run_action(case, dict(action='ask', reason='currency', order_id='ORD-102',
                    order_message_id=old, order_evidence='ORD-102'))
                self.assertNotIn('order', case['remembered_details'])
                self.assertEqual(case['proposals'], [])
                self.assertEqual(self.gateway.refund_posts, [])

    def test_withdrawal_barrier_prevents_resurrecting_old_amount_after_neutral_reply(self):
        case = self.remember_request()
        case = self.reply(case, 'Do not refund. Cancel this request.')
        case = self.reply(case, 'I have a question about the packaging.')
        old = self.case['messages'][0]['id']
        case = self.run_action(case, dict(action='ask', reason='currency', refund_type='partial', amount='10',
                                         amount_message_id=old, amount_evidence='10'))
        self.assertNotIn('amount', case['remembered_details'])
        self.assertEqual(case['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(case['stage'], 'awaiting_clarification')
        self.assertEqual(case['agent_turns'][-1]['model_steps'], 0)
        self.assertTrue(any(e['kind'] == 'agent_waiting_for_customer' and
                            e['details']['reason'] == 'paused_request' for e in case['events']))
        self.assertEqual(self.gateway.refund_posts, [])

    def test_foreign_order_and_fabricated_evidence_are_not_remembered(self):
        mid = self.case['messages'][0]['id']
        case = self.run_action(self.case, dict(action='ask', reason='amount', order_id='ORD-201',
                                             order_message_id=mid, order_evidence='ORD-102'))
        self.assertEqual(case['remembered_details'], {})
        case = self.reply(case, 'Please use my Friday purchase.')
        case = self.run_action(case, dict(action='ask', reason='amount', order_id='ORD-102',
                                         order_message_id='invented', order_evidence='Friday'))
        self.assertEqual(case['remembered_details'], {})

    def test_currency_only_is_saved_and_correction_drops_it(self):
        case = self.reply(self.case, 'USD')
        mid = case['messages'][-1]['id']
        case = self.run_action(case, dict(action='ask', reason='amount', currency_message_id=mid, currency_evidence='USD'))
        self.assertEqual(case['remembered_details']['currency']['currency'], 'usd')
        case = self.reply(case, 'EUR instead.')
        self.assertEqual(case['remembered_details'], {})

    def test_invalid_optional_full_refund_claim_does_not_break_clarification(self):
        case = self.store.create_case(self.customer, 'Refund the headphones I bought last week.')
        mid = case['messages'][0]['id']
        planner = ScriptedPlanner([dict(action='search_orders', query='headphones', last_week=True),
            dict(action='ask', reason='purchase', refund_type='full',
                 amount_message_id=mid, amount_evidence='Refund')])
        case = AgentRunner(self.store, self.gateway, planner).run(self.customer, case['id'])
        self.assertEqual(case['stage'], 'awaiting_clarification')
        self.assertIn('ORD-101', case['question'])
        self.assertIn('ORD-102', case['question'])
        self.assertEqual(case['remembered_details'], {})
        self.assertEqual(case['proposals'], [])
        self.assertTrue(any(e['kind'] == 'case_memory_rejected' and
                            e['details']['error'] == 'invalid_full_evidence' for e in case['events']))
        self.assertEqual(self.gateway.refund_posts, [])

    def test_remembered_panel_and_export_are_customer_scoped(self):
        case = self.remember_request()
        client = create_app(Settings(self.path)).test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id'] = self.customer
        path = f"/demo/cases/{case['id']}"
        page = client.get(path)
        self.assertIn(b'Remembered details', page.data)
        self.assertIn(b'currency not yet specified', page.data)
        self.assertEqual(client.get(path + '/record').get_json()['case']['remembered_details'], case['remembered_details'])
        with client.session_transaction() as session:
            session['demo_customer_id'] = 'memory:b'
        self.assertEqual(client.get(path).status_code, 404)
