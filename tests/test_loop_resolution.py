"""Regressions for the observed v2 failures; no live providers."""

import unittest
from unittest.mock import Mock

from payresolve.agent import AgentRunner
from payresolve.agent_contract import AgentAction, ground_order, ground_amount, request_is_paused
from payresolve.cases import CaseService, CaseStore
from payresolve.stripe_gateway import SandboxError
from tests import test_agent


class LoopResolutionTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    run_actions = test_agent.AgentTests.run_actions
    clarify = test_agent.AgentTests.clarify
    reply = test_agent.AgentTests.reply

    def test_foreign_order_asks_for_owned_purchase_without_provider_or_second_model_call(self):
        self.gateway.get_payment = Mock(wraps=self.gateway.get_payment)
        result, planner = self.run_actions(self.case, [dict(action='inspect_payment', order_id='ORD-201')])
        self.assertEqual(result['stage'], 'awaiting_clarification')
        self.assertEqual(result['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(len(planner.contexts), 1)
        self.assertIn("couldn't find", result['question'])
        self.gateway.get_payment.assert_not_called()
        self.assertEqual(self.gateway.refund_posts, [])

    def test_repeated_search_stops_before_second_dispatch_even_if_unused_fields_change(self):
        result, planner = self.run_actions(self.case, [dict(action='search_orders', query='headphones'),
            dict(action='search_orders', query='headphones', amount='5')])
        self.assertEqual(len(planner.contexts), 2)
        self.assertEqual(result['agent_turns'][-1]['tool_calls'], 1)
        self.assertEqual(result['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(result['stage'], 'awaiting_clarification')
        self.assertTrue(any(e['kind'] == 'agent_loop_stopped' for e in result['events']))

    def test_corrected_proposal_evidence_is_allowed_after_a_rejected_call(self):
        case = self.reply(self.clarify(), 'Friday. Refund $10.')
        def invalid(context):
            return {**test_agent.proposal_action(context), 'amount_message_id': 'fabricated'}
        result, _ = self.run_actions(case, [invalid, test_agent.proposal_action])
        self.assertEqual(result['stage'], 'awaiting_confirmation')
        self.assertFalse(any(e['kind'] == 'agent_loop_stopped' for e in result['events']))

    def prepare_refund(self, *, lose_response=False):
        case = self.reply(self.clarify(), 'Friday. Refund $10.')
        case, _ = self.run_actions(case, [test_agent.proposal_action])
        self.gateway.lose_response = lose_response
        return CaseService(self.store, self.gateway).confirm(self.customer, case['id'], case['revision'], case['version'])

    def test_status_finishes_after_one_observation_without_an_extra_model_call(self):
        case = self.reply(self.prepare_refund(), 'What is the refund status?')
        self.gateway.get_refund = Mock(wraps=self.gateway.get_refund)
        result, planner = self.run_actions(case, [dict(action='get_refund_status')])
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(result['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(result['stage'], 'verified')
        self.gateway.get_refund.assert_called_once()
        self.assertIn(result['refund_id'], result['messages'][-1]['content'])
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_unknown_outcome_status_finishes_without_replaying_refund(self):
        case = self.reply(self.prepare_refund(lose_response=True), 'Check the existing refund.')
        result, planner = self.run_actions(case, [dict(action='get_refund_status')])
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(result['stage'], 'needs_reconciliation')
        self.assertEqual(result['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(len(self.gateway.refund_posts), 1)
        self.assertIn('needs reconciliation', result['messages'][-1]['content'])

    def test_unconfirmed_status_does_not_claim_an_existing_operation(self):
        result, _ = self.run_actions(self.case, [dict(action='get_refund_status')])
        self.assertIsNone(result['operation_id'])
        self.assertIn('No refund has been authorized', result['messages'][-1]['content'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_pause_preserves_order_across_restart_then_accepts_new_amount(self):
        case = self.reply(self.clarify(), 'Refund USD 15 from ORD-102.')
        case, _ = self.run_actions(case, [lambda context: test_agent.proposal_action(
            context, amount='15', amount_quote='15', order_quote='ORD-102')])
        old = case
        case = self.reply(case, 'Before I decide: do not refund yet; I am reviewing the offer.')
        planner = Mock(model='must-not-be-called')
        case = AgentRunner(self.store, self.gateway, planner).run(self.customer, case['id'])
        planner.step.assert_not_called()
        self.assertEqual(case['stage'], 'awaiting_clarification')
        self.assertNotIn('amount', case['remembered_details'])
        self.assertEqual(case['remembered_details']['order']['order_id'], 'ORD-102')
        with self.assertRaises(SandboxError):
            self.store.claim(self.customer, case['id'], old['revision'], old['version'])
        case = self.reply(case, 'I want to keep the headphones and will choose a new amount.')
        original = next(m for m in case['messages'] if m['role']=='customer' and 'ORD-102' in m['content'])
        ground_order(AgentAction(action='inspect_payment', order_id='ORD-102',
                     order_message_id=original['id'], order_evidence='ORD-102'), case, self.store.catalog, self.customer)
        case = self.reply(case, 'There is a cosmetic scratch, but they still work. Wait for my amount.')
        case = AgentRunner(self.store, self.gateway, planner).run(self.customer, case['id'])
        planner.step.assert_not_called()
        self.store = CaseStore(self.path)
        case = self.reply(self.store.snapshot(self.customer, case['id']), 'Refund USD 6 from ORD-102.')
        case, _ = self.run_actions(case, [lambda context: test_agent.proposal_action(
            context, amount='6', amount_quote='6', order_quote='ORD-102')])
        self.assertEqual(case['stage'], 'awaiting_confirmation')
        self.assertEqual(case['amount_minor'], 600)
        self.assertGreater(case['revision'], old['revision'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_general_product_mention_does_not_weaken_real_order_corrections(self):
        case = self.reply(self.clarify(), 'Refund USD 15 from ORD-102.')
        message = case['messages'][-1]
        action = AgentAction(action='inspect_payment', order_id='ORD-102',
                             order_message_id=message['id'], order_evidence='ORD-102')
        for text in ('The other headphones instead.', 'Monday instead.', 'Use ORD-101.', 'October 26 instead.'):
            with self.subTest(text=text):
                changed = {**case, 'messages': [*case['messages'], dict(id='correction', role='customer', content=text)]}
                with self.assertRaises(SandboxError):
                    ground_order(action, changed, self.store.catalog, self.customer)

    def test_pause_survives_neutral_turns_restart_and_both_context_modes(self):
        for mode in ('history', 'structured'):
            with self.subTest(mode=mode):
                case = self.store.create_case(self.customer, 'Refund USD 10 from ORD-102.')
                case, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
                    c, order_quote='ORD-102', amount_quote='10')], context_mode=mode)
                old = case
                case = self.reply(case, 'Wait, do not refund yet.')
                for text in ('The packaging was scratched.', 'I am checking the packaging.',
                             'The full price was USD 59.99.'):
                    case = self.reply(case, text)
                    self.store = CaseStore(self.path)
                    planner = Mock(model='must-not-be-called')
                    case = AgentRunner(self.store, self.gateway, planner, context_mode=mode).run(self.customer, case['id'])
                    planner.step.assert_not_called()
                    self.assertEqual(case['stage'], 'awaiting_clarification')
                    self.assertEqual(len(case['proposals']), 1)
                    self.assertEqual(case['remembered_details']['order']['order_id'], 'ORD-102')
                with self.assertRaises(SandboxError):
                    self.store.claim(self.customer, case['id'], old['revision'], old['version'])
                case = self.reply(case, 'Refund USD 6 from ORD-102.')
                case, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
                    c, order_quote='ORD-102', amount='6', amount_quote='6')], context_mode=mode)
                self.assertEqual(case['stage'], 'awaiting_confirmation')
                self.assertEqual(case['amount_minor'], 600)
                self.assertGreater(case['revision'], old['revision'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_price_occurrences_are_rejected_but_explicit_refund_amounts_work(self):
        for text, quote, amount, valid in (
            ('ORD-102 cost USD 59.99; I want a refund.', '59.99', '59.99', False),
            ('I paid $59.99 for ORD-102. Please refund it.', '$59.99', '59.99', False),
            ('The original price was USD 59.99.', '59.99', '59.99', False),
            ('Refund ORD-102 which cost USD 59.99.', '59.99', '59.99', False),
            ('I bought ORD-102 for USD 59.99. I want a refund.', '59.99', '59.99', False),
            ('USD 59.99 was the purchase price; I want a refund.', '59.99', '59.99', False),
            ('I paid $59.99; refund $10.', '$10', '10', True),
            ('I paid $59.99, refund $59.99.', '59.99', '59.99', True),
            ('Refund USD 59.99, the original price.', '59.99', '59.99', True),
            ('10', '10', '10', True),
        ):
            with self.subTest(text=text):
                action = AgentAction(action='propose_refund', refund_type='partial', amount=amount,
                                     amount_message_id='m', amount_evidence=quote)
                case = {'messages':[dict(id='m', role='customer', content=text)]}
                if valid:
                    self.assertGreater(ground_amount(action, case), 0)
                else:
                    with self.assertRaises(SandboxError) as caught:
                        ground_amount(action, case)
                    self.assertEqual(caught.exception.code, 'invalid_amount_evidence')

    def test_purchase_price_cannot_create_card_then_customer_supplies_amount(self):
        case = self.store.create_case(self.customer, 'ORD-102 cost USD 59.99; I want a refund.')
        case, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount='59.99', amount_quote='59.99'),
            dict(action='ask', reason='amount')])
        self.assertEqual(case['stage'], 'awaiting_clarification')
        self.assertEqual(case['proposals'], [])
        case = self.reply(case, 'Refund USD 10.')
        case, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount_quote='10')])
        self.assertEqual(case['amount_minor'], 1000)
        self.assertEqual(case['stage'], 'awaiting_confirmation')
        self.assertEqual(self.gateway.refund_posts, [])

    def test_refund_all_uses_remaining_balance_and_revision_still_requires_confirmation(self):
        case = self.store.create_case(self.customer, 'Refund all of ORD-102.')
        order = self.store.catalog.order(self.customer, 'ORD-102')
        self.gateway.payments[order['payment_id']].update(amount_refunded=1000, refundable_amount=4999)
        def full(context):
            message = next(m for m in context['messages'] if m['role']=='customer' and 'all' in m['content'])
            return dict(action='propose_refund', order_id='ORD-102', order_message_id=message['id'],
                        order_evidence='ORD-102', refund_type='full', amount_message_id=message['id'], amount_evidence='all')
        case, _ = self.run_actions(case, [full])
        self.assertEqual(case['amount_minor'], 4999)
        old = case
        case = self.reply(case, 'Actually refund USD 10.')
        case, _ = self.run_actions(case, [lambda c: test_agent.proposal_action(
            c, order_quote='ORD-102', amount_quote='10')])
        self.assertEqual(case['amount_minor'], 1000)
        self.assertGreater(case['revision'], old['revision'])
        with self.assertRaises(SandboxError):
            self.store.claim(self.customer, case['id'], old['revision'], old['version'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_all_does_not_mean_full_refund_when_describing_items_and_old_amount_stays_revoked(self):
        for text in ('All the headphones were scratched.', 'All the boxes were full.',
                     'I checked all purchases; refund ORD-102.'):
            with self.subTest(text=text), self.assertRaises(SandboxError):
                ground_amount(AgentAction(action='propose_refund', refund_type='full',
                    amount_message_id='m', amount_evidence='All' if text.startswith('All') else 'all'),
                    {'messages':[dict(id='m', role='customer', content=text)]})
        case = {'messages':[dict(id='old', role='customer', content='Refund USD 10.'),
                            dict(id='pause', role='customer', content='Wait.'),
                            dict(id='new', role='customer', content='USD 6.')]}
        self.assertFalse(request_is_paused(case))
        with self.assertRaises(SandboxError) as caught:
            ground_amount(AgentAction(action='propose_refund', refund_type='partial', amount='10',
                                     amount_message_id='old', amount_evidence='10'), case)
        self.assertEqual(caught.exception.code, 'stale_amount_evidence')
