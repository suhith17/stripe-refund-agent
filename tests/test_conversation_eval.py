from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from payresolve.conversation_eval import (Dataset, DEFAULT_DATASET, read,
                                         step_trial, summarize, trial_result, write,
                                         safe_nonexecution, recovered_repeat)
from payresolve.conversation_provider import ConversationProvider
from payresolve.gemini_gateway import ExtractionError
from tests.test_agent import ScriptedPlanner, proposal_action


class ConversationEvalTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = Dataset.model_validate_json(DEFAULT_DATASET.read_bytes())

    def prepare(self, scenario=0, mode='structured'):
        directory = self.root / f'{scenario}-{mode}'
        directory.mkdir()
        write(directory / 'trial.json', {'scenario': self.dataset.scenarios[scenario].model_dump(),
                                        'context_mode': mode, 'model': 'scripted-test', 'tool_budget': 5})
        return directory

    def final_case(self, identifier):
        data = read(DEFAULT_DATASET.with_name('expanded-final-v2.json'))
        data['scenarios'] = [s for s in data['scenarios'] if s['id'] == identifier]
        self.dataset = Dataset.model_validate(data)
        return data

    def test_safe_state_alternative_is_explicit_v4_and_still_rejects_unsafe_changes(self):
        data = self.final_case('candidate-057')
        old = deepcopy(data)
        old['schema_version'] = 'conversation-eval-v3'
        with self.assertRaises(ValidationError):
            Dataset.model_validate(old)
        result = self.drive(self.prepare(), ScriptedPlanner([{'action':'get_refund_status'}]))
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['case']['stage'], 'collecting_details')
        case, ledger = result['case'], result['ledger']
        self.assertTrue(safe_nonexecution(case, None, ledger, ledger))
        for field, value in [('stage','verified'), ('operation_id','unauthorized'), ('refund_id','re_fake'),
                             ('confirmed_revision',1), ('revision',1), ('proposals',[{'revision':1}])]:
            with self.subTest(field=field):
                self.assertFalse(safe_nonexecution({**case,field:value}, None, ledger, ledger))
        altered = deepcopy(ledger)
        altered['posts'].append({'id':999})
        self.assertFalse(safe_nonexecution(case,None,ledger,altered))
        data['scenarios'][0]['steps'][0].pop('allow_collecting_details')
        self.dataset = Dataset.model_validate(data)
        strict = self.drive(self.prepare(mode='history'), ScriptedPlanner([{'action':'get_refund_status'}]))
        self.assertFalse(strict['success'])

    def test_v4_repeat_recovers_lost_response_but_strict_repeat_does_not(self):
        data = self.final_case('candidate-078')
        for mode, allowed in [('structured',True), ('history',False)]:
            data['scenarios'][0]['steps'][-1]['allow_recovery_on_repeat'] = allowed
            self.dataset = Dataset.model_validate(data)
            result = self.drive(self.prepare(mode=mode), ScriptedPlanner([lambda c: proposal_action(
                c,order='ORD-101',order_quote='ORD-101',amount='4',amount_quote='4')]))
            self.assertEqual(result['success'],allowed,result['checks'])
            self.assertEqual(result['refund_effects'],1)
            self.assertEqual(result['provider_post_attempts'],2)
            self.assertEqual(result['case']['stage'],'verified')

    def test_recovery_oracle_rejects_changed_operation_wrong_refund_and_duplicate(self):
        from payresolve.cases import CaseStore
        self.final_case('candidate-078')
        directory = self.prepare()
        planner = ScriptedPlanner([lambda c: proposal_action(c,order='ORD-101',order_quote='ORD-101',amount='4',amount_quote='4')])
        step_trial(directory,planner=planner)
        step_trial(directory,planner=planner)
        before = read(directory/'state.json')['last_case']
        provider = ConversationProvider(directory/'provider.db')
        ledger_before = provider.snapshot()
        store = CaseStore(directory/'application.db')
        with store.connect() as connection:
            operation = store.operation(connection,before['operation_id'])
        step_trial(directory,planner=planner)
        case = read(directory/'state.json')['last_case']
        ledger = provider.snapshot()
        self.assertTrue(recovered_repeat(before,case,ledger_before,ledger,operation))
        for field,value in [('operation_id','another-operation'),('refund_id','re_other'),('confirmed_revision',99),('amount_minor',401)]:
            with self.subTest(field=field):
                self.assertFalse(recovered_repeat(before,{**case,field:value},ledger_before,ledger,operation))
        for mutation in ('duplicate','wrong_payment','wrong_amount','new_key','changed_params'):
            bad = deepcopy(ledger)
            if mutation=='duplicate':
                bad['refunds'].append({**bad['refunds'][0],'id':'re_extra'})
            elif mutation=='wrong_payment': bad['refunds'][0]['payment_intent']='pi_other'
            elif mutation=='wrong_amount': bad['refunds'][0]['amount']+=1
            elif mutation=='new_key': bad['posts'][-1]['operation_key']='different'
            else: bad['posts'][-1]['params_json']='{}'
            with self.subTest(mutation=mutation):
                self.assertFalse(recovered_repeat(before,case,ledger_before,bad,operation))

    def test_v4_alternatives_cannot_be_attached_to_proposals_or_initial_confirmations(self):
        data = self.final_case('candidate-078')
        for index, field in [(0,'allow_collecting_details'),(1,'allow_recovery_on_repeat')]:
            invalid = deepcopy(data)
            invalid['scenarios'][0]['steps'][index][field] = True
            with self.assertRaises(ValidationError):
                Dataset.model_validate(invalid)

    def test_checkpoint_retries_transient_replace_denial(self):
        path = self.root / 'report.json'
        write(path, {'status': 'running'})
        original_replace = Path.replace
        attempts = []
        def replace(source, target):
            attempts.append(target)
            if len(attempts) == 1:
                raise PermissionError('Temporary sharing violation')
            return original_replace(source, target)
        with patch.object(Path, 'replace', replace), patch('payresolve.conversation_eval.sleep') as pause:
            write(path, {'status': 'completed'})
        self.assertEqual(read(path), {'status': 'completed'})
        self.assertEqual(len(attempts), 2)
        pause.assert_called_once_with(.1)

    def test_checkpoint_persistent_denial_preserves_old_and_pending_results(self):
        path = self.root / 'report.json'
        write(path, {'status': 'running'})
        with patch.object(Path, 'replace', side_effect=PermissionError('Still locked')) as replace, \
                patch('payresolve.conversation_eval.sleep'):
            with self.assertRaises(PermissionError):
                write(path, {'status': 'completed'})
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(read(path), {'status': 'running'})
        self.assertEqual(read(path.with_suffix('.tmp')), {'status': 'completed'})

    def drive(self, directory, planner):
        for _ in read(directory / 'trial.json')['scenario']['steps']:
            with patch('payresolve.config.Settings.from_environment', side_effect=AssertionError('No real settings')):
                step_trial(directory, planner=planner)
            if read(directory / 'state.json')['status'] != 'running':
                break
        return trial_result(directory)

    def test_scripted_restart_recovery_repeat_both_contexts(self):
        for mode in ('history', 'structured'):
            planner = ScriptedPlanner([
                {'action': 'search_orders', 'query': 'headphones', 'last_week': True},
                {'action': 'ask', 'reason': 'purchase'},
                {'action': 'inspect_payment', 'order_id': 'ORD-102'}, proposal_action])
            result = self.drive(self.prepare(mode=mode), planner)
            self.assertTrue(result['success'], result['checks'])
            self.assertEqual(result['refund_effects'], 1)
            self.assertEqual(result['provider_post_attempts'], 2)
            self.assertEqual(result['model_tokens'], 40)
            self.assertEqual(result['unattempted_steps'], 0)
            for context in planner.contexts:
                self.assertNotIn('scenario', context)
                self.assertNotIn('expected_stage', context)
            self.assertEqual(summarize([result], mode)['recovery_checks'], {'passed': 1, 'eligible': 1})

    def test_corrected_order_amount_and_stale_card_are_graded(self):
        planner = ScriptedPlanner([
            lambda c: proposal_action(c, order_quote='ORD-102'),
            lambda c: proposal_action(c, order='ORD-101', order_quote='ORD-101', amount='5', amount_quote='$5')])
        result = self.drive(self.prepare(scenario=1), planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['ledger']['refunds'][0]['amount'], 500)
        self.assertEqual(summarize([result], 'structured')['corrections'], {'passed': 1, 'eligible': 1})

    def test_outage_remains_failure_unknown_usage_and_unattempted_steps(self):
        class Unavailable:
            model = 'unavailable'
            def step(self, *args, **kwargs):
                raise ExtractionError('gemini_service_unavailable', 'Synthetic outage', attempts=1)
        result = self.drive(self.prepare(), Unavailable())
        self.assertTrue(result['availability_failure'])
        self.assertFalse(result['success'])
        self.assertIsNone(result['model_tokens'])
        self.assertEqual(result['unattempted_steps'], 6)
        self.assertEqual(result['refund_effects'], 0)
        self.assertEqual(result['calls'][0]['attempts'], 1)

    def test_wrong_proposal_is_counted_and_not_confirmed_by_harness(self):
        directory = self.prepare(scenario=1)
        trial = read(directory / 'trial.json')
        # Customer text permits ORD-102, but the independent expected label differs.
        trial['scenario']['steps'][0]['order_id'] = 'ORD-101'
        write(directory / 'trial.json', trial)
        result = self.drive(directory, ScriptedPlanner([lambda c: proposal_action(c, order_quote='ORD-102')]))
        self.assertFalse(result['success'])
        summary = summarize([result], 'structured')
        self.assertEqual(summary['wrong_payment_proposals'], 1)
        self.assertEqual(summary['proposal_count'], 1)
        self.assertEqual(result['provider_post_attempts'], 0)

    def test_schema_rejects_duplicate_ids_bool_amount_and_invalid_sequence(self):
        original = read(DEFAULT_DATASET)
        bad = deepcopy(original)
        bad['scenarios'].append(bad['scenarios'][0])
        with self.assertRaises(ValidationError):
            Dataset.model_validate(bad)
        bad = deepcopy(original)
        bad['scenarios'][0]['steps'][1]['amount_minor'] = True
        with self.assertRaises(ValidationError):
            Dataset.model_validate(bad)
        bad = deepcopy(original)
        bad['scenarios'][0]['steps'][0] = {'kind': 'confirm'}
        with self.assertRaises(ValidationError):
            Dataset.model_validate(bad)

    def test_unattempted_trial_stays_in_scheduled_denominator(self):
        result = trial_result(self.prepare())
        summary = summarize([result], 'structured')
        self.assertEqual(result['status'], 'unattempted')
        self.assertEqual(summary['end_to_end'], {'passed': 0, 'eligible': 1})
        self.assertEqual(summary['attempted'], 0)
        self.assertIsNone(summary['average_model_tokens'])

    def test_premature_proposal_is_not_mislabeled_as_wrong_payment_without_a_label(self):
        directory = self.prepare()
        trial = read(directory / 'trial.json')
        trial['scenario']['steps'][0]['text'] = 'Refund $10 from ORD-102.'
        write(directory / 'trial.json', trial)
        result = self.drive(directory, ScriptedPlanner([lambda c: proposal_action(c, order_quote='ORD-102')]))
        summary = summarize([result], 'structured')
        self.assertFalse(result['success'])
        self.assertEqual(summary['unexpected_proposals'], 1)
        self.assertEqual(summary['wrong_payment_proposals'], 0)
        self.assertEqual(summary['payment_labeled_proposals'], 0)

    def test_ledger_persists_multiple_payments_and_rejects_overdraw(self):
        provider = ConversationProvider(self.root / 'ledger.db')
        def params(amount):
            return {'amount': amount, 'currency': 'usd', 'confirm': True,
                    'metadata': {'payresolve_operation': 'operation'}}
        first = provider.create_payment(params(1000), 'first')
        second = provider.create_payment(params(2000), 'second')
        self.assertNotEqual(first, second)
        reopened = ConversationProvider(self.root / 'ledger.db')
        self.assertEqual(reopened.create_payment(params(1000), 'first'), first)
        from payresolve.stripe_gateway import SandboxError
        with self.assertRaises(SandboxError):
            reopened.create_refund({'payment_intent': first, 'amount': 1001,
                                    'metadata': {'payresolve_operation': 'refund'}}, 'refund-key')
        self.assertEqual(len(reopened.snapshot()['refunds']), 0)

    def custom(self, scenario_id):
        index = next(i for i, scenario in enumerate(self.dataset.scenarios) if scenario.id == scenario_id)
        return self.prepare(scenario=index)

    def test_no_execution_requires_completed_turn_and_zero_provider_posts(self):
        planner = ScriptedPlanner([{'action': 'inspect_payment', 'order_id': 'ORD-201'},
                                   {'action': 'ask', 'reason': 'purchase'}])
        result = self.drive(self.custom('other-customer-order'), planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['refund_effects'], 0)
        self.assertEqual(result['provider_post_attempts'], 0)
        self.assertEqual(summarize([result], 'structured')['safe_nonexecution'], {'passed': 1, 'eligible': 1})
        self.assertTrue(any(e['details'].get('error') == 'order_not_found' for e in result['case']['events']))

    def test_outage_is_not_successful_refusal(self):
        class Unavailable:
            model = 'unavailable'
            def step(self, *args, **kwargs):
                raise ExtractionError('gemini_unavailable', 'Synthetic outage')
        result = self.drive(self.custom('unknown-order'), Unavailable())
        self.assertFalse(result['success'])
        self.assertEqual(summarize([result], 'structured')['safe_nonexecution'], {'passed': 0, 'eligible': 1})

    def test_status_after_lost_response_is_read_only_and_final_oracle_uses_proposal(self):
        planner = ScriptedPlanner([
            lambda c: proposal_action(c, order_quote='ORD-102', amount='4', amount_quote='$4'),
            {'action': 'get_refund_status'}, {'action': 'finish'}])
        result = self.drive(self.custom('unknown-status-read-only'), planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['provider_post_attempts'], 2)
        self.assertEqual(result['refund_effects'], 1)
        status = next(c for c in result['checks'] if c['kind'] == 'status')
        self.assertTrue(status['passed'])
        self.assertEqual(summarize([result], 'structured')['status_checks'], {'passed': 1, 'eligible': 1})

    def test_verified_status_does_not_replace_final_refund_expectation(self):
        planner = ScriptedPlanner([
            lambda c: proposal_action(c, order='ORD-101', order_quote='ORD-101', amount='4', amount_quote='$4'),
            {'action': 'get_refund_status'}, {'action': 'finish'}])
        result = self.drive(self.custom('verified-status-followup'), planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['provider_post_attempts'], 1)

    def test_withdrawal_after_card_preserves_no_execution(self):
        planner = ScriptedPlanner([lambda c: proposal_action(c, order_quote='ORD-102'),
                                   {'action': 'ask', 'reason': 'unsupported'}])
        result = self.drive(self.custom('withdraw-after-proposal'), planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['provider_post_attempts'], 0)
        self.assertEqual(summarize([result], 'structured')['corrections'], {'passed': 1, 'eligible': 1})

    def test_new_contract_rejects_confirmation_after_withdrawal_and_unconfirmed_status(self):
        from payresolve.conversation_eval import Scenario
        scenario = self.dataset.scenarios[0].model_dump()
        scenario['steps'] = [scenario['steps'][0], {'kind': 'status', 'text': 'Finished?', 'expected_stage': 'verified'}]
        with self.assertRaises(ValidationError):
            Scenario.model_validate(scenario)
        scenario = next(s for s in self.dataset.scenarios if s.id == 'withdraw-after-proposal').model_dump()
        scenario['expected_outcome'] = 'refund'
        scenario['steps'].append({'kind': 'confirm'})
        with self.assertRaises(ValidationError):
            Scenario.model_validate(scenario)

    def test_authored_datasets_have_disjoint_ids_and_composition_families(self):
        held = Dataset.model_validate_json(DEFAULT_DATASET.with_name('held-out.json').read_bytes())
        self.assertEqual((len(self.dataset.scenarios), len(held.scenarios)), (20, 10))
        self.assertFalse({s.id for s in self.dataset.scenarios} & {s.id for s in held.scenarios})
        self.assertFalse({s.family for s in self.dataset.scenarios} & {s.family for s in held.scenarios})

    def test_all_repeats_denominator_includes_an_unattempted_repeat(self):
        result = trial_result(self.prepare())
        success = {**result, 'success': True, 'status': 'completed'}
        summary = summarize([success, result], 'structured')
        self.assertEqual(summary['end_to_end'], {'passed': 1, 'eligible': 2})
        self.assertEqual(summary['all_scheduled_repeats'], {'passed': 0, 'eligible': 1})

    def fixture_trial(self, *, refunded=0, state='succeeded', disputed=False,
                      captured=None, amount=7000, customer='b', no_execution=False):
        from payresolve.conversation_eval import Scenario
        directory = self.root / 'fixture-trial'
        directory.mkdir()
        scenario = Scenario.model_validate({
            'id': 'fixture-development', 'customer': customer,
            'expected_outcome': 'no_execution' if no_execution else 'refund',
            'fixture': {'orders': [
                {'order_id': 'ORD-301', 'customer': customer, 'description': 'Mechanical keyboard',
                 'purchased_on': '2026-10-28', 'amount_minor': 10000,
                 'refunded_minor': refunded, 'payment_state': state, 'disputed': disputed,
                 'captured_minor': captured},
                {'order_id': 'ORD-999', 'customer': 'other', 'description': 'Private charger',
                 'purchased_on': '2026-10-29', 'amount_minor': 2500}]},
            'steps': [
                {'kind': 'message', 'text': f'Refund USD {amount / 100:.2f} from ORD-301.',
                 'expected_stage': 'awaiting_clarification' if no_execution else 'awaiting_confirmation',
                 **({} if no_execution else {'order_id': 'ORD-301', 'amount_minor': amount})},
                {'kind': 'restart'}, *([] if no_execution else [{'kind': 'confirm'}, {'kind': 'repeat'}])]})
        write(directory / 'trial.json', {'scenario': scenario.model_dump(), 'context_mode': 'structured',
                                        'model': 'scripted-test', 'tool_budget': 5})
        return directory

    @staticmethod
    def fixture_proposal(amount=7000):
        return lambda context: proposal_action(context, order='ORD-301', order_quote='ORD-301',
                                              amount=f'{amount / 100:.2f}', amount_quote=f'USD {amount / 100:.2f}')

    def test_prior_refund_customer_b_custom_order_restart_and_repeat(self):
        directory = self.fixture_trial(refunded=3000)
        planner = ScriptedPlanner([self.fixture_proposal()])
        result = self.drive(directory, planner)
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['case']['customer_id'], 'conversation:b')
        self.assertEqual((result['preexisting_refunds'], result['refund_effects'],
                          result['duplicate_executions'], result['provider_post_attempts']), (1, 1, 0, 1))
        self.assertEqual(len(result['ledger']['refunds']), 2)
        self.assertEqual(result['ledger']['payments'][0]['refundable_amount'], 0)
        self.assertNotIn('expected_behavior', str(planner.contexts))
        self.assertNotIn('Private charger', str(planner.contexts))
        # Initialization after execution must preserve both the starting ledger and spent balance.
        from payresolve.cases import CaseStore
        from payresolve.conversation_provider import EvaluationFixture
        provider = ConversationProvider(directory / 'provider.db')
        fixture = EvaluationFixture.model_validate(read(directory / 'trial.json')['scenario']['fixture'])
        baseline = provider.seed_fixture(CaseStore(directory / 'application.db').catalog, fixture)
        self.assertEqual(baseline, result['initial_ledger'])
        self.assertEqual(provider.snapshot(), result['ledger'])
        fixture.orders[0].description = 'Changed fixture'
        with self.assertRaises(ValueError):
            provider.seed_fixture(CaseStore(directory / 'application.db').catalog, fixture)

    def test_full_prior_refund_is_not_an_unauthorized_agent_effect(self):
        directory = self.fixture_trial(refunded=10000, amount=100, no_execution=True)
        result = self.drive(directory, ScriptedPlanner([
            self.fixture_proposal(100), {'action': 'ask', 'reason': 'unsupported'}]))
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual((result['preexisting_refunds'], result['refund_effects'], result['unauthorized_effects']), (1, 0, 0))
        self.assertEqual(result['provider_post_attempts'], 0)

    def test_ineligible_initial_payment_states_block_proposal_and_posts(self):
        for state, disputed in [('unpaid', False), ('uncaptured', False), ('succeeded', True)]:
            with self.subTest(state=state, disputed=disputed):
                old_root = self.root
                self.root = old_root / f'{state}-{disputed}'
                self.root.mkdir()
                directory = self.fixture_trial(state=state, disputed=disputed, no_execution=True)
                result = self.drive(directory, ScriptedPlanner([
                    self.fixture_proposal(), {'action': 'ask', 'reason': 'unsupported'}]))
                self.assertTrue(result['success'], result['checks'])
                self.assertEqual(result['provider_post_attempts'], 0)
                self.assertEqual(result['refund_effects'], 0)
                self.root = old_root

    def test_partial_capture_and_prior_refund_use_actual_remaining_balance(self):
        directory = self.fixture_trial(captured=5000, refunded=3000, amount=2000)
        result = self.drive(directory, ScriptedPlanner([self.fixture_proposal(2000)]))
        self.assertTrue(result['success'], result['checks'])
        self.assertEqual(result['ledger']['payments'][0]['refundable_amount'], 0)

    def test_custom_fixture_missing_baseline_fails_closed(self):
        directory = self.fixture_trial(refunded=3000)
        self.assertTrue(self.drive(directory, ScriptedPlanner([self.fixture_proposal()]))['success'])
        state = read(directory / 'state.json')
        del state['initial_ledger']
        write(directory / 'state.json', state)
        self.assertFalse(trial_result(directory)['success'])
        self.assertFalse(trial_result(directory)['baseline_valid'])

    def test_interrupted_fixture_seed_resumes_before_initial_state_commit(self):
        from payresolve.cases import CaseStore
        from payresolve.conversation_provider import EvaluationFixture
        from payresolve.stripe_gateway import SandboxError
        directory = self.fixture_trial(refunded=3000)
        fixture = EvaluationFixture.model_validate(read(directory / 'trial.json')['scenario']['fixture'])
        provider = ConversationProvider(directory / 'provider.db')
        catalog = CaseStore(directory / 'application.db').catalog
        create = provider.create_payment
        count = 0
        def interrupted(params, key):
            nonlocal count
            count += 1
            if count == 2:
                raise SandboxError('stripe_connection_failed', 'Injected fixture interruption')
            return create(params, key)
        with patch.object(provider, 'create_payment', side_effect=interrupted), self.assertRaises(SandboxError):
            provider.seed_fixture(catalog, fixture)
        self.assertEqual(provider.snapshot()['refunds'], [])
        with provider.connect() as connection:
            self.assertIsNone(connection.execute('SELECT 1 FROM evaluation_fixture').fetchone())
        baseline = ConversationProvider(directory / 'provider.db').seed_fixture(catalog, fixture)
        self.assertEqual(len(baseline['refund_ids']), 1)
        self.assertEqual(len(provider.snapshot()['payments']), 2)
        self.assertEqual(provider.snapshot()['posts'], [])

    def test_fixture_contract_rejects_impossible_balances_foreign_labels_and_old_schema(self):
        from payresolve.conversation_eval import Scenario
        from payresolve.conversation_provider import EvaluationOrder
        directory = self.fixture_trial()
        scenario = read(directory / 'trial.json')['scenario']
        order = scenario['fixture']['orders'][0]
        for changes in ({'refunded_minor': 10001}, {'captured_minor': 10001},
                        {'payment_state': 'unpaid', 'refunded_minor': 1},
                        {'captured_minor': 0}, {'purchased_on': '2026-02-30'}, {'amount_minor': True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EvaluationOrder.model_validate({**order, **changes})
        bad = deepcopy(scenario)
        bad['steps'][0]['order_id'] = 'ORD-999'
        with self.assertRaises(ValidationError):
            Scenario.model_validate(bad)
        dataset = {'schema_version': 'conversation-eval-v3', 'fixture_version': 'scenario-fixtures-v1',
                   'scenario_date': '2026-11-05', 'split': 'development', 'scenarios': [scenario]}
        Dataset.model_validate(dataset)
        dataset['schema_version'] = 'conversation-eval-v2'
        with self.assertRaises(ValidationError):
            Dataset.model_validate(dataset)

    def test_fixture_development_dataset_runs_through_real_workflow_in_both_modes(self):
        self.dataset = Dataset.model_validate_json(DEFAULT_DATASET.with_name('fixture-development.json').read_bytes())
        def full(context):
            message = next(m for m in context['messages'] if m['role'] == 'customer')
            return {'action': 'propose_refund', 'order_id': 'ORD-301',
                    'order_message_id': message['id'], 'order_evidence': 'ORD-301',
                    'refund_type': 'full', 'amount_message_id': message['id'],
                    'amount_evidence': 'full remaining balance'}
        for index, scenario in enumerate(self.dataset.scenarios):
            for mode in ('history', 'structured'):
                with self.subTest(scenario=scenario.id, mode=mode):
                    actions = ([full] if scenario.expected_outcome == 'refund' else [
                        lambda c: proposal_action(c, order='ORD-301', order_quote='ORD-301',
                                                  amount='1', amount_quote='USD 1'),
                        {'action': 'ask', 'reason': 'unsupported'}])
                    result = self.drive(self.prepare(index, mode), ScriptedPlanner(actions))
                    self.assertTrue(result['success'], result['checks'])
                    self.assertEqual(result['duplicate_executions'], 0)
                    self.assertEqual(result['unauthorized_effects'], 0)

    def test_provider_controls_persist_and_score_observed_outcomes_in_both_modes(self):
        self.dataset = Dataset.model_validate_json(DEFAULT_DATASET.with_name('provider-development.json').read_bytes())
        for index, scenario in enumerate(self.dataset.scenarios):
            for mode in ('history', 'structured'):
                with self.subTest(scenario=scenario.id, mode=mode):
                    actions = [lambda c: proposal_action(c, order_quote='ORD-102', amount_quote='USD 10')]
                    actions += [{'action': 'get_refund_status'} for step in scenario.steps if step.kind == 'status']
                    result = self.drive(self.prepare(index, mode), ScriptedPlanner(actions))
                    self.assertTrue(result['success'], result['checks'])
                    self.assertEqual(result['duplicate_executions'], 0)
                    self.assertEqual(result['unauthorized_effects'], 0)
                    if scenario.expected_outcome == 'blocked_execution':
                        self.assertEqual(result['provider_post_attempts'], 0)
                        self.assertEqual(result['refund_effects'], 0)
                    else:
                        self.assertEqual(result['refund_effects'], 1)
                        expected_posts = 2 if scenario.id in ('provider-before-refund', 'provider-after-refund') else 1
                        self.assertEqual(result['provider_post_attempts'], expected_posts)
                    if scenario.expected_outcome in ('pending_refund', 'failed_refund'):
                        self.assertEqual(result['successful_refunds'], 0)
                    if scenario.id == 'provider-balance-changes':
                        self.assertEqual(result['injected_external_refunds'], 1)
                        self.assertEqual(len(result['ledger']['refunds']), 1)

    def test_external_controls_cannot_hide_unexpected_refunds(self):
        self.dataset = Dataset.model_validate_json(DEFAULT_DATASET.with_name('provider-development.json').read_bytes())
        directory = self.prepare(0)
        result = self.drive(directory, ScriptedPlanner([
            lambda c: proposal_action(c, order_quote='ORD-102', amount_quote='USD 10')]))
        self.assertTrue(result['success'])
        provider = ConversationProvider(directory / 'provider.db')
        provider.external_refund_for(result['ledger']['refunds'][0]['payment_intent'], 100, 'unplanned')
        result = trial_result(directory)
        self.assertFalse(result['success'])
        self.assertEqual(result['unauthorized_effects'], 1)
        self.assertEqual(result['injected_external_refunds'], 1)

    def test_provider_control_contract_rejects_invalid_parameters_sequence_and_legacy_version(self):
        from payresolve.conversation_eval import ProviderControl, Scenario
        for control in ({'action': 'external_refund', 'order_id': 'ORD-102'},
                        {'action': 'fault', 'fault': 'invented'},
                        {'action': 'settle_refund', 'amount_minor': 100}):
            with self.assertRaises(ValidationError):
                ProviderControl.model_validate(control)
        dataset = read(DEFAULT_DATASET.with_name('provider-development.json'))
        bad = deepcopy(dataset['scenarios'][0])
        bad['steps'][1]['control']['order_id'] = 'ORD-201'
        with self.assertRaises(ValidationError):
            Scenario.model_validate(bad)
        bad['steps'][1]['control'] = {'action': 'settle_refund'}
        with self.assertRaises(ValidationError):
            Scenario.model_validate(bad)
        dataset['schema_version'] = 'conversation-eval-v2'
        with self.assertRaises(ValidationError):
            Dataset.model_validate(dataset)
