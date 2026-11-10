"""Post-study edge cases: adversarial planner actions and simulated payments."""

import unittest
from unittest.mock import Mock, patch

from payresolve import create_app
from payresolve.agent import AgentRunner
from payresolve.cases import CaseService, CaseStore
from payresolve.config import Settings
from payresolve.stripe_gateway import SandboxError
from tests import test_agent


class AdversarialCaseTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    run_actions = test_agent.AgentTests.run_actions
    reply = test_agent.AgentTests.reply

    def proposal(self, case=None, amount=1000):
        case = case or self.case
        return CaseService(self.store, self.gateway).propose(
            self.customer, case['id'], 'ORD-102', amount, 'usd', case['version'])

    def confirm(self, case):
        return CaseService(self.store, self.gateway).confirm(
            self.customer, case['id'], case['revision'], case['version'])

    def client(self):
        client = create_app(Settings(self.path)).test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id'] = self.customer
            token = session['csrf']
        return client, token

    def test_two_tabs_cannot_confirm_an_old_amount(self):
        old = self.proposal()
        tab_a, csrf_a = self.client()
        tab_b, csrf_b = self.client()
        path = f"/demo/cases/{old['id']}"
        self.assertEqual(tab_a.get(path).status_code, 200)
        self.assertEqual(tab_b.get(path).status_code, 200)
        revised = self.proposal(self.reply(old, 'Actually refund USD 5.'), 500)
        with patch('payresolve.dashboard.StripeSandbox', return_value=self.gateway):
            rejected = tab_a.post(path+'/confirm', data=dict(csrf=csrf_a, confirm='yes',
                version=old['version'], revision=old['revision']))
            self.assertGreaterEqual(rejected.status_code, 400)
            self.assertEqual(self.gateway.refund_posts, [])
            accepted = tab_b.post(path+'/confirm', data=dict(csrf=csrf_b, confirm='yes',
                version=revised['version'], revision=revised['revision']))
        self.assertEqual(accepted.status_code, 303)
        self.assertEqual(self.gateway.refund_posts[0][0]['amount'], 500)

    def test_external_refund_after_proposal_never_silently_shrinks_amount(self):
        case = self.proposal()
        payment = self.gateway.payments[case['proposals'][-1]['payment_id']]
        payment.update(amount_refunded=5799, refundable_amount=200)
        result = self.confirm(case)
        self.assertEqual(result['stage'], 'needs_reconciliation')
        self.assertEqual(result['amount_minor'], 1000)
        self.assertEqual(result['last_error'], 'invalid_refund_amount')
        self.assertEqual(self.gateway.refund_posts, [])
        self.assertIn('available balance', AgentRunner.status_message(result))

    def test_inflight_ten_dollar_proposal_cannot_replace_three_dollar_correction(self):
        case = self.reply(self.case, 'Refund USD 10 from ORD-102.')
        def stale_response(context):
            action = test_agent.proposal_action(context, order_quote='ORD-102', amount_quote='10')
            self.reply(self.store.snapshot(self.customer, case['id']), 'Actually refund USD 3.')
            return action
        result, _ = self.run_actions(case, [stale_response])
        self.assertEqual(result['agent_turns'][-1]['status'], 'superseded')
        self.assertEqual(result['proposals'], [])
        result, _ = self.run_actions(result, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount='3', amount_quote='3')])
        self.assertEqual(result['amount_minor'], 300)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_negated_full_refund_requests_targeted_partial_clarification(self):
        case = self.reply(self.case, "Don't refund the full amount from ORD-102; refund USD 5.")
        planner = Mock(model='not-needed')
        result = AgentRunner(self.store, self.gateway, planner).run(self.customer, case['id'])
        planner.step.assert_not_called()
        self.assertEqual(result['stage'], 'awaiting_clarification')
        self.assertIn('partial refund', result['question'])
        case = self.reply(result, 'Refund USD 5 from ORD-102.')
        result, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount='5', amount_quote='5')])
        self.assertEqual(result['amount_minor'], 500)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_item_count_is_not_money_even_when_currency_is_present(self):
        case = self.reply(self.case, 'Refund the 2 headphones from ORD-102; they arrived 3 days late. Currency USD.')
        result, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount='2', amount_quote='2'), dict(action='ask', reason='amount')])
        self.assertEqual(result['proposals'], [])
        self.assertEqual(result['stage'], 'awaiting_clarification')
        self.assertTrue(any(e['details'].get('error') == 'invalid_amount_evidence' for e in result['events']))

    def test_delivery_weekday_cannot_be_used_as_purchase_evidence(self):
        case, _ = self.run_actions(self.case, [dict(action='search_orders', query='headphones'),
                                               dict(action='ask', reason='purchase')])
        case = self.reply(case, 'The headphones arrived Friday. Refund USD 5.')
        result, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='Friday', amount='5', amount_quote='5'), dict(action='ask', reason='purchase')])
        self.assertEqual(result['proposals'], [])
        case = self.reply(result, 'I bought them Monday, ORD-101. Refund USD 5.')
        result, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order='ORD-101', order_quote='ORD-101', amount='5', amount_quote='5')])
        self.assertEqual(result['selected_order'], 'ORD-101')

    def test_amount_quote_boundaries_and_real_money_among_counts(self):
        from payresolve.agent_contract import AgentAction, ground_amount
        for text, quote, amount in (
                ('ORD-102 refund in USD.', '102', '102'),
                ('Refund USD2.', '2', '2'),
                ('Refund USD 2.50.', '2', '2'),
                ('The items arrived 3 days late. Currency USD.', '3', '3')):
            with self.subTest(text=text), self.assertRaises(SandboxError):
                ground_amount(AgentAction(action='propose_refund', refund_type='partial', amount=amount,
                    amount_message_id='source', amount_evidence=quote),
                    {'messages': [dict(id='source', role='customer', content=text)]})
        for text, quote in (('Refund $2.', '2'), ('For the 2 headphones, refund USD 2.', '2'),
                            ('Refund USD 2, please.', '2')):
            with self.subTest(text=text):
                self.assertEqual(ground_amount(AgentAction(action='propose_refund', refund_type='partial',
                    amount='2', amount_message_id='source', amount_evidence=quote),
                    {'messages': [dict(id='source', role='customer', content=text)]}), 200)

    def test_customer_switch_denies_old_confirmation_and_record(self):
        case = self.proposal()
        client, csrf = self.client()
        path = f"/demo/cases/{case['id']}"
        with client.session_transaction() as session:
            session['demo_customer_id'] = 'agent:b'
        with patch('payresolve.dashboard.StripeSandbox', return_value=self.gateway):
            response = client.post(path+'/confirm', data=dict(csrf=csrf, confirm='yes',
                version=case['version'], revision=case['revision']))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(case['proposals'][-1]['payment_id'].encode(), response.data)
        self.assertEqual(client.get(path+'/record').status_code, 404)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_old_source_after_same_order_restatement_is_refreshed(self):
        for tool in ('inspect_payment', 'propose_refund'):
            with self.subTest(tool=tool):
                first = self.store.create_case(self.customer, 'Refund USD 5 from ORD-102.')
                original_source = first['messages'][-1]['id']
                case = self.reply(first, 'Refund USD 4 from ORD-102.')
                latest_source = case['messages'][-1]['id']
                def old_source(context):
                    action = test_agent.proposal_action(context, order_quote='ORD-102', amount='4', amount_quote='4')
                    return {**action, 'action': tool, 'order_message_id': original_source}
                def propose_after_inspection(context):
                    observation = context['this_turn_observations'][-1]
                    self.assertNotIn('error', observation)
                    self.assertIn('payment', observation)
                    return test_agent.proposal_action(context, order_quote='ORD-102', amount='4', amount_quote='4')
                actions = [old_source, propose_after_inspection] if tool == 'inspect_payment' else [old_source]
                result, planner = self.run_actions(case, actions)
                self.assertEqual(result['stage'], 'awaiting_confirmation')
                self.assertEqual(len(planner.contexts), len(actions))
                self.assertEqual(result['remembered_details']['order']['message_id'], latest_source)
                self.assertEqual(result['amount_minor'], 400)
                self.assertEqual(len(result['proposals']), 1)
                self.assertEqual(self.gateway.refund_posts, [])

    def test_cancel_after_unknown_submission_explains_limit_and_only_reads_status(self):
        case = self.proposal()
        self.gateway.lose_response = True
        unknown = self.confirm(case)
        with self.assertRaises(SandboxError):
            self.store.cancel(self.customer, case['id'], unknown['version'])
        case = self.reply(unknown, 'Cancel this refund now.')
        result, _ = self.run_actions(case, [dict(action='get_refund_status')])
        self.assertEqual(result['operation_id'], unknown['operation_id'])
        self.assertEqual(result['stage'], 'needs_reconciliation')
        self.assertIn("can't cancel", result['messages'][-1]['content'])
        self.assertEqual(len(self.gateway.refund_posts), 1)
        recovered = CaseService(CaseStore(self.path), self.gateway).resume(self.customer, case['id'])
        self.assertEqual(recovered['stage'], 'verified')
        self.assertEqual(len(self.gateway.refunds), 1)

    def test_multiple_purchase_request_is_not_silently_reduced_to_one(self):
        case = self.reply(self.case, 'Refund USD 5 from both ORD-101 and ORD-102.')
        result, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount='5', amount_quote='5'), dict(action='ask', reason='purchase')])
        self.assertEqual(result['proposals'], [])
        self.assertIn('one purchase per case', result['question'])

    def test_second_case_shows_existing_case_but_allows_separately_confirmed_partial_refund(self):
        first = self.confirm(self.proposal())
        second = self.store.create_case(self.customer, 'Another USD 5 from ORD-102.')
        second = self.proposal(second, 500)
        client, _ = self.client()
        page = client.get(f"/demo/cases/{second['id']}")
        self.assertIn(b'Other cases for this purchase', page.data)
        self.assertIn(first['id'].encode(), page.data)
        self.assertIn(b'additional refund', page.data)
        with self.assertRaises(SandboxError):
            self.store.related_cases('agent:b', second['id'])
        # Distinct, explicit confirmations authorize distinct partial refunds.
        confirmed = self.confirm(second)
        self.assertNotEqual(confirmed['operation_id'], first['operation_id'])
        self.assertEqual(len(self.gateway.refunds), 2)
        self.confirm(second)
        self.assertEqual(len(self.gateway.refunds), 2)
