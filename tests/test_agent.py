from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from payresolve import create_app
from payresolve.agent import AgentRunner, build_context
from payresolve.cases import CaseStore, CaseService
from payresolve.catalog import seed_catalog
from payresolve.config import Settings
from payresolve.gemini_gateway import ExtractionError
from payresolve.model_runtime import ChatPlanner
from payresolve.stripe_gateway import SandboxError
from tests.test_workflow import FakeStripe


class ScriptedPlanner:
    model = 'scripted-test'

    def __init__(self, actions):
        self.actions, self.contexts = iter(actions), []

    def step(self, context, **kwargs):
        self.contexts.append(context)
        action = next(self.actions)
        return {'action':action(context) if callable(action) else action,
                'usage':{'total_tokens':10}, 'attempts':1, 'latency_ms':1}


def proposal_action(context, *, amount='10', amount_quote='$10', order='ORD-102', order_quote='Friday'):
    messages = context['messages']
    selected = next(m for m in reversed(messages) if m['role'] == 'customer' and order_quote in m['content'])
    money = next(m for m in reversed(messages) if m['role'] == 'customer' and amount_quote in m['content'])
    currency = next(m for m in reversed(messages) if m['role'] == 'customer' and ('$' in m['content'] or 'USD' in m['content']))
    return dict(action='propose_refund', order_id=order, order_message_id=selected['id'], order_evidence=order_quote,
                refund_type='partial', amount=amount, amount_message_id=money['id'], amount_evidence=amount_quote,
                currency_message_id=currency['id'], currency_evidence='$' if '$' in currency['content'] else 'USD')


class AgentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'agent.db'
        self.store = CaseStore(self.path)
        self.gateway = FakeStripe()
        seed_catalog(self.store.catalog, self.gateway, 'agent')
        self.customer = 'agent:a'
        self.case = self.store.create_case(self.customer, 'Refund the headphones I bought last week.')

    def run_actions(self, case, actions, **kwargs):
        planner = ScriptedPlanner(actions)
        result = AgentRunner(self.store, self.gateway, planner, **kwargs).run(self.customer, case['id'])
        return result, planner

    def clarify(self):
        result, _ = self.run_actions(self.case, [dict(action='search_orders',query='headphones',last_week=True),dict(action='ask',reason='purchase')])
        return result

    def test_backup_proposal_keeps_confirmation_boundary_and_visible_model_audit(self):
        case = self.clarify()
        case = self.reply(case, 'October 30. Just refund $10.')
        primary = Mock(model='gemini-primary')
        primary.step.side_effect = ExtractionError('gemini_service_unavailable', 'Unavailable', attempts=1)
        backup = ScriptedPlanner([dict(action='inspect_payment', order_id='ORD-102'),
                                  lambda context: proposal_action(context, order_quote='October 30')])
        planner = ChatPlanner(primary, backup)
        result = AgentRunner(self.store, self.gateway, planner).run(self.customer, case['id'])
        self.assertEqual(result['stage'], 'awaiting_confirmation')
        self.assertEqual(result['selected_order'], 'ORD-102')
        self.assertEqual(result['amount_minor'], 1000)
        self.assertIsNone(result['operation_id'])
        self.assertEqual(self.gateway.refund_posts, [])
        switches = [e for e in result['events'] if e['kind'] == 'agent_model_fallback']
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0]['details']['to_model'], backup.model)
        self.assertEqual(primary.step.call_count, 1)
        from payresolve.case_view import present_case
        self.assertIn('Switched to the backup model', [a['title'] for a in present_case(result)['activity']])

    def reply(self, case, text):
        return self.store.message(self.customer,case['id'],text,case['version'],status_followup=True)

    def test_ambiguous_request_short_reply_restart_proposal_confirmation_and_status(self):
        case = self.clarify()
        self.assertEqual(case['stage'], 'awaiting_clarification')
        self.assertIn('ORD-101',case['question'])
        self.assertIn('ORD-102',case['question'])
        self.store = CaseStore(self.path)
        case = self.reply(case,'Friday. Just refund $10.')
        case, planner = self.run_actions(case,[dict(action='inspect_payment',order_id='ORD-102'),proposal_action])
        self.assertEqual(case['stage'],'awaiting_confirmation')
        self.assertEqual(case['amount_minor'],1000)
        self.assertEqual(case['selected_order'],'ORD-102')
        self.assertEqual(self.gateway.refund_posts,[])
        self.assertTrue(any(e['kind']=='agent_tool_result' for e in case['events']))
        self.assertFalse(any(m['role']=='customer_form' for m in case['messages']))
        confirmed=CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        followup=self.reply(confirmed,'What is the status?')
        result,_=self.run_actions(followup,[dict(action='get_refund_status'),dict(action='finish')])
        self.assertEqual(result['stage'],'verified')
        self.assertIn(result['refund_id'],result['messages'][-1]['content'])
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_correction_uses_new_amount_and_old_card_is_invalid(self):
        case=self.reply(self.clarify(),'Friday. Just refund $10.')
        old,_=self.run_actions(case,[proposal_action])
        case=self.reply(old,'Make it $5 instead.')
        case,_=self.run_actions(case,[lambda c:proposal_action(c,amount='5',amount_quote='$5')])
        self.assertEqual(case['amount_minor'],500)
        self.assertEqual(case['revision'],2)
        with self.assertRaises(SandboxError):
            self.store.claim(self.customer,old['id'],old['revision'],old['version'])

    def test_ignored_order_correction_and_fabricated_quote_rejected(self):
        case=self.reply(self.clarify(),'Friday. Just refund $10.')
        case=self.reply(case,'Monday instead.')
        result,_=self.run_actions(case,[proposal_action,dict(action='ask',reason='purchase')])
        self.assertEqual(result['proposals'],[])
        self.assertTrue(any(e['details'].get('error')=='conflicting_order_evidence' for e in result['events']))
        case=self.reply(result,'Monday. Refund $10.')
        def fabricated(c):
            return {**proposal_action(c,order='ORD-101',order_quote='Monday'), 'amount_message_id':'invented'}
        result,_=self.run_actions(case,[fabricated,dict(action='ask',reason='amount')])
        self.assertEqual(result['proposals'],[])

    def test_currency_supplied_in_separate_reply(self):
        case=self.reply(self.clarify(),'Friday. Refund 10.')
        case,_=self.run_actions(case,[dict(action='ask',reason='currency')])
        case=self.reply(case,'USD')
        result,_=self.run_actions(case,[lambda c:proposal_action(c,amount_quote='10')])
        self.assertEqual(result['amount_minor'],1000)

    def test_remembered_order_full_price_reports_existing_full_refund(self):
        case = self.reply(self.clarify(), 'Wireless headphones ORD-101 · 2026-10-26')
        mid = case['messages'][-1]['id']
        case, _ = self.run_actions(case, [dict(action='ask', reason='amount', order_id='ORD-101',
            order_message_id=mid, order_evidence='ORD-101')])
        payment = self.gateway.payments[self.store.catalog.order(self.customer, 'ORD-101')['payment_id']]
        payment.update(amount_refunded=3999, refundable_amount=0)
        self.store = CaseStore(self.path)
        case = self.reply(self.store.snapshot(self.customer, case['id']), 'full price')
        result, planner = self.run_actions(case, [dict(action='inspect_payment', order_id='ORD-101')])
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(result['remembered_details']['order']['order_id'], 'ORD-101')
        self.assertIn('ORD-101 is already fully refunded', result['question'])
        self.assertNotIn('Which purchase', result['question'])
        self.assertEqual(result['agent_turns'][-1]['status'], 'completed')
        self.assertEqual(result['proposals'], [])
        self.assertIsNone(result['operation_id'])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_lowercase_order_quote_and_full_price_create_correct_full_proposal(self):
        case = self.reply(self.clarify(), 'ord-101')
        mid = case['messages'][-1]['id']
        case, _ = self.run_actions(case, [dict(action='inspect_payment', order_id='ORD-101',
            order_message_id=mid, order_evidence='ord-101'), dict(action='ask', reason='amount')])
        self.assertEqual(case['remembered_details']['order']['order_id'], 'ORD-101')
        case = self.reply(case, 'full price')
        amount_mid = case['messages'][-1]['id']
        result, _ = self.run_actions(case, [dict(action='propose_refund', order_id='ORD-101',
            order_message_id=mid, order_evidence='ord-101', refund_type='full',
            amount_message_id=amount_mid, amount_evidence='full price')])
        self.assertEqual(result['selected_order'], 'ORD-101')
        self.assertEqual(result['amount_minor'], 3999)
        self.assertEqual(result['stage'], 'awaiting_confirmation')
        self.assertEqual(self.gateway.refund_posts, [])
        corrected = self.reply(result, 'ord-102 instead')
        result, _ = self.run_actions(corrected, [dict(action='propose_refund', order_id='ORD-101',
            order_message_id=mid, order_evidence='ord-101', refund_type='full',
            amount_message_id=amount_mid, amount_evidence='full price'), dict(action='ask', reason='purchase')])
        self.assertEqual(len(result['proposals']), 1)  # Only the invalidated historical proposal.
        self.assertTrue(any(e['details'].get('error') == 'conflicting_order_evidence' for e in result['events']))

    def test_model_cannot_inspect_alternative_after_grounded_selection(self):
        case = self.reply(self.clarify(), 'ORD-101')
        mid = case['messages'][-1]['id']
        case, _ = self.run_actions(case, [dict(action='ask', reason='amount', order_id='ORD-101',
            order_message_id=mid, order_evidence='ORD-101')])
        case = self.reply(case, 'full price')
        self.gateway.get_payment = Mock(wraps=self.gateway.get_payment)
        result, _ = self.run_actions(case, [dict(action='inspect_payment', order_id='ORD-102'),
                                          dict(action='ask', reason='amount')])
        self.gateway.get_payment.assert_not_called()
        self.assertTrue(any(e['details'].get('error') == 'conflicting_order_evidence' for e in result['events']))

    def test_incomplete_proposal_can_repair_from_tool_feedback_without_new_customer_turn(self):
        for mode in ('history', 'structured'):
            with self.subTest(mode=mode):
                self.case = self.store.create_case(self.customer, 'Refund headphones last week.')
                case = self.reply(self.clarify(), 'Friday. Just refund $10.')

                def repair(context):
                    feedback = context['this_turn_observations'][-1]
                    self.assertEqual(feedback['error'], 'invalid_evidence')
                    self.assertIn('amount', feedback['missing_fields'])
                    self.assertIn('order_evidence', feedback['missing_fields'])
                    self.assertIn('currency_message_id', feedback['missing_fields'])
                    self.assertEqual(feedback['rejected_arguments']['order_id'], 'ORD-102')
                    self.assertEqual(self.store.snapshot(self.customer, case['id'])['proposals'], [])
                    return proposal_action(context)

                result, _ = self.run_actions(case, [dict(action='propose_refund',
                    order_id='ORD-102', refund_type='partial'), repair], context_mode=mode)
                self.assertEqual(result['stage'], 'awaiting_confirmation')
                self.assertEqual(result['amount_minor'], 1000)
                self.assertEqual(result['agent_turns'][-1]['tool_calls'], 2)
                self.assertIsNone(result['operation_id'])
                self.assertEqual(self.gateway.refund_posts, [])

    def test_fabricated_proposal_evidence_stays_rejected_after_feedback(self):
        case = self.reply(self.clarify(), 'Friday. Just refund $10.')
        def fabricated(context):
            return {**proposal_action(context), 'amount_message_id': 'invented'}
        def clarify_after_rejection(context):
            self.assertEqual(context['this_turn_observations'][-1]['error'], 'invalid_evidence')
            self.assertEqual(context['this_turn_observations'][-1]['missing_fields'], [])
            return dict(action='ask', reason='amount')
        result, _ = self.run_actions(case, [fabricated, clarify_after_rejection])
        self.assertEqual(result['proposals'], [])
        self.assertEqual(self.gateway.refund_posts, [])

    def test_tool_budget_is_bounded_and_case_survives(self):
        actions=[dict(action='search_orders',query=query) for query in ('headphones', 'wireless', '')]
        result,planner=self.run_actions(self.case,actions,tool_budget=2)
        self.assertEqual(result['agent_turns'][-1]['tool_calls'],2)
        self.assertEqual(result['agent_turns'][-1]['status'],'budget_exhausted')
        self.assertEqual(len(planner.contexts),3)
        self.assertEqual(self.gateway.refund_posts,[])

    def test_customer_scope_and_unavailable_provider(self):
        self.gateway.get_payment=Mock(wraps=self.gateway.get_payment)
        case,_=self.run_actions(self.case,[dict(action='inspect_payment',order_id='ORD-201'),dict(action='ask',reason='purchase')])
        self.gateway.get_payment.assert_not_called()
        class Unavailable:
            model='unavailable'
            def step(self,*a,**kw):
                raise ExtractionError('gemini_service_unavailable','Down')
        case=self.reply(case,'Try again')
        result=AgentRunner(self.store,self.gateway,Unavailable()).run(self.customer,case['id'])
        self.assertEqual(result['agent_turns'][-1]['status'],'gemini_service_unavailable')
        self.assertIn('case is saved',result['messages'][-1]['content'])

    def test_model_cannot_call_execution_or_replay_unknown_refund(self):
        case,_=self.run_actions(self.case,[dict(action='execute_refund')])
        self.assertEqual(case['proposals'],[])
        case=self.reply(case,'Friday. Refund $10.')
        with self.store.connect() as c:
            c.execute('UPDATE refund_cases SET candidates_json=? WHERE id=?', ('["ORD-101","ORD-102"]',case['id']))
        case,_=self.run_actions(case,[proposal_action])
        self.gateway.lose_response=True
        case=CaseService(self.store,self.gateway).confirm(self.customer,case['id'],case['revision'],case['version'])
        self.assertEqual(len(self.gateway.refund_posts),1)
        case=self.reply(case,'Is it finished?')
        result,_=self.run_actions(case,[dict(action='get_refund_status'),dict(action='finish')])
        self.assertEqual(result['stage'],'needs_reconciliation')
        self.assertEqual(len(self.gateway.refund_posts),1)

    def test_new_message_fences_inflight_model_result(self):
        def interrupted(context):
            current=self.store.snapshot(self.customer,self.case['id'])
            self.reply(current,'Actually wait; do not refund.')
            return dict(action='search_orders',query='headphones')
        result,_=self.run_actions(self.case,[interrupted])
        self.assertEqual(result['agent_turns'][-1]['status'],'superseded')
        self.assertEqual(result['agent_turns'][-1]['tool_calls'],0)

    def test_active_turn_is_not_replayed_and_expired_turn_can_resume(self):
        def check_active(context):
            with self.assertRaises(SandboxError) as caught:
                AgentRunner(self.store,self.gateway,ScriptedPlanner([])).run(self.customer,self.case['id'])
            self.assertEqual(caught.exception.code,'agent_turn_running')
            return dict(action='ask',reason='purchase')
        result,_=self.run_actions(self.case,[check_active])
        with self.store.connect() as c:
            c.execute("UPDATE agent_turns SET status='running',expires_at=0,case_version=? WHERE id=?",(result['version'],result['agent_turns'][-1]['id']))
        result,_=self.run_actions(result,[dict(action='ask',reason='purchase')])
        self.assertEqual(result['agent_turns'][0]['status'],'interrupted')

    def test_context_modes_share_execution_but_differ_in_memory(self):
        case=self.reply(self.clarify(),'Friday. Refund $10.')
        history=build_context(self.store,self.customer,case,'history',[],5)
        structured=build_context(self.store,self.customer,case,'structured',[],5)
        self.assertNotIn('memory',history)
        self.assertIn('memory',structured)
        self.assertEqual(history['execution'],structured['execution'])
        result,_=self.run_actions(case,[proposal_action],context_mode='history')
        self.assertEqual(result['stage'],'awaiting_confirmation')

    def test_chat_http_automatically_runs_tools_and_preserves_confirmation_boundary(self):
        app=create_app(Settings(self.path))
        client=app.test_client()
        client.get('/demo')
        with client.session_transaction() as session:
            session['demo_customer_id']=self.customer
            token=session['csrf']
        planner=ScriptedPlanner([dict(action='search_orders',query='headphones'),dict(action='ask',reason='purchase'),proposal_action])
        with patch('payresolve.dashboard.GeminiPlanner',return_value=planner),patch('payresolve.dashboard.StripeSandbox',return_value=self.gateway):
            response=client.post('/demo/cases',data={'csrf':token,'message':'Refund headphones last week'})
            self.assertEqual(response.status_code,303)
            case_id=response.headers['Location'].rsplit('/',1)[-1]
            case=self.store.snapshot(self.customer,case_id)
            response=client.post(f'/demo/cases/{case_id}/message',data={'csrf':token,'version':case['version'],'message':'Friday. Just refund $10.'})
        self.assertEqual(response.status_code,303)
        self.assertIn(b'Confirm refund',client.get(response.headers['Location']).data)
        self.assertEqual(self.gateway.refund_posts,[])

    def test_truncated_numeric_quote_and_ignored_bare_amount_correction_are_rejected(self):
        case=self.reply(self.clarify(),'Friday. Refund $100.')
        result,_=self.run_actions(case,[proposal_action,dict(action='ask',reason='amount')])
        self.assertEqual(result['proposals'],[])
        case=self.reply(result,'Friday. Refund $10.')
        case=self.reply(case,'5')
        result,_=self.run_actions(case,[proposal_action,dict(action='ask',reason='amount')])
        self.assertEqual(result['proposals'],[])
        self.assertTrue(any(e['details'].get('error')=='stale_amount_evidence' for e in result['events']))

    def test_full_refund_uses_fresh_balance_and_no_synthetic_customer_message(self):
        case=self.reply(self.case,'Refund the full remaining amount from ORD-102.')
        def full(context):
            mid=context['messages'][-1]['id']
            return dict(action='propose_refund', order_id='ORD-102', order_message_id=mid,
                        order_evidence='ORD-102',refund_type='full',amount_message_id=mid,amount_evidence='full remaining amount')
        result,_=self.run_actions(case,[full])
        self.assertEqual(result['amount_minor'],5999)
        self.assertFalse(any(m['role']=='customer_form' for m in result['messages']))
        self.assertEqual(self.gateway.refund_posts,[])

    def test_expired_budget_stops_before_tool_dispatch(self):
        def expired(context):
            with self.store.connect() as c:
                c.execute("UPDATE agent_turns SET expires_at=0 WHERE case_id=?",(self.case['id'],))
            return dict(action='search_orders',query='headphones')
        result,_=self.run_actions(self.case,[expired])
        self.assertEqual(result['agent_turns'][-1]['status'],'agent_time_limit')
        self.assertEqual(result['agent_turns'][-1]['model_steps'],1)
        self.assertEqual(result['agent_turns'][-1]['tool_calls'],0)


if __name__=='__main__':
    unittest.main()
