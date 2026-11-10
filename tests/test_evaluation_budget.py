"""Quota accounting and transport checks; all providers are offline."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from google import genai
import httpx

from payresolve.agent import GeminiPlanner
from payresolve.config import Settings
from payresolve.conversation_eval import DEFAULT_DATASET, read, run, step_trial, write
from payresolve.evaluation_budget import EvaluationBudget, BudgetedPacer, study_budget
from payresolve.gemini_gateway import ExtractionError
from tools.continue_conversation_study import run as continue_run, finish_untouched


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = 'gemini-test'

    def budget(self, limit=1):
        budget = EvaluationBudget(self.root / 'budget.db', self.model, limit)
        budget.initialize()
        return budget

    def test_atomic_reservations_and_restart_keep_unknown_calls_charged(self):
        budget = self.budget(4)
        def reserve(index):
            reopened = EvaluationBudget(budget.path, self.model, 4)
            try:
                reopened.reserve(self.model, f'trial-{index}')
                return 'reserved'
            except ExtractionError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(results.count('reserved'), 4)
        self.assertEqual(results.count('evaluation_budget_exhausted'), 16)
        # No completion callback: an interrupted request still consumes its reservation.
        self.assertEqual(EvaluationBudget(budget.path, self.model, 4).snapshot()['remaining'], 0)
        with self.assertRaises(FileExistsError):
            budget.initialize()

    def test_missing_mismatched_and_invalid_ledgers_fail_closed(self):
        budget = self.budget()
        for other in (EvaluationBudget(self.root / 'missing.db', self.model, 1),
                      EvaluationBudget(budget.path, self.model, 2),
                      EvaluationBudget(budget.path, 'gemini-other', 1)):
            with self.assertRaises(ExtractionError) as caught:
                other.reserve(other.model, 'trial')
            self.assertEqual(caught.exception.code, 'evaluation_budget_unavailable')
        self.assertFalse((self.root / 'missing.db').exists())
        with self.assertRaises(ValueError):
            study_budget(self.root, {'owner_run_id': '../escape', 'model': self.model, 'limit': 1})
        with self.assertRaises(ValueError):
            EvaluationBudget(budget.path, self.model, True)

    def test_installed_sdk_stops_at_cap_before_second_transport_send(self):
        budget = self.budget()
        pacer = Mock()
        pacer.wait.return_value = 0
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(503, json={'error': {'code': 503}})
        original = genai.Client
        def client(**options):
            options['http_options']['client_args']['transport'] = httpx.MockTransport(respond)
            return original(**options)
        with patch('payresolve.gemini_gateway.genai.Client', side_effect=client):
            for expected in ('gemini_service_unavailable', 'evaluation_budget_exhausted'):
                planner = GeminiPlanner('fake-key', self.model,
                                        pacer=BudgetedPacer(pacer, budget, 'trial'))
                with self.assertRaises(ExtractionError) as caught:
                    planner.step({'messages': []}, remaining_seconds=20)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(len(seen), 1)
        self.assertEqual(budget.snapshot()['reserved_requests'], 1)
        self.assertEqual(caught.exception.attempts, 0)

    def test_pacing_failure_does_not_reserve_an_unsent_request(self):
        budget = self.budget()
        pacer = Mock()
        pacer.wait.side_effect = ExtractionError('gemini_rate_limited', 'Synthetic cooldown')
        with self.assertRaises(ExtractionError):
            BudgetedPacer(pacer, budget, 'trial').wait(self.model, time.monotonic() + 10)
        self.assertEqual(budget.snapshot()['reserved_requests'], 0)

    def configure_run(self, cap):
        dataset = read(DEFAULT_DATASET)
        scenario = {'id': 'first', 'expected_outcome': 'no_execution', 'steps': [
            {'kind': 'message', 'text': 'Refund headphones.', 'expected_stage': 'awaiting_clarification'},
            {'kind': 'restart'}]}
        second = deepcopy(scenario)
        second['id'] = 'second'
        dataset['scenarios'] = [scenario, second]
        path = self.root / 'dataset.json'
        write(path, dataset)
        return SimpleNamespace(dataset=path, scenario=None, limit=2, mode='history', seed=0,
                               repeats=1, max_requests=cap)

    def settings(self):
        return Settings(self.root / 'unused.db', gemini_api_key='fake-key', gemini_model=self.model,
                        model_pacing_path=self.root / 'pacing.db')

    @staticmethod
    def worker(command, **kwargs):
        step_trial(Path(command[-1]))
        return SimpleNamespace(returncode=0)

    def fake_planner(self, action=None, error=None):
        # Run-loop tests exercise bookkeeping; the separate installed-SDK test
        # above proves that BudgetedPacer is invoked before an actual send.
        model = self.model
        class Planner:
            def __init__(self):
                self.model = model
                self.pacer = Mock()
                self.pacer.wait.return_value = 0
            def step(self, context, **kwargs):
                self.pacer.wait(self.model, time.monotonic() + 20)
                if error:
                    raise ExtractionError(error, 'Synthetic outage', attempts=1)
                return {'action': action or {'action': 'ask', 'reason': 'purchase'},
                        'usage': {'total_tokens': 10}, 'attempts': 1}
        return Planner()

    def test_mid_turn_budget_stop_is_not_a_model_failure_or_new_trial(self):
        args = self.configure_run(1)
        with patch('payresolve.conversation_eval.PROJECT_ROOT', self.root), \
                patch('payresolve.conversation_eval.fingerprint', return_value='frozen'), \
                patch('payresolve.config.Settings.from_environment', return_value=self.settings()), \
                patch('payresolve.conversation_eval.configured_planner',
                      side_effect=lambda *a, **k: self.fake_planner({'action': 'search_orders', 'query': 'headphones'})), \
                patch('payresolve.conversation_eval.subprocess.run', side_effect=self.worker), patch('builtins.print'):
            self.assertEqual(run(args), 1)
        root = next((self.root / 'instance/conversations').iterdir())
        report = read(root / 'report.json')
        self.assertEqual(report['stop_reason'], 'evaluation_budget_exhausted')
        self.assertEqual(report['budget_usage']['reserved_requests'], 1)
        self.assertEqual([row['status'] for row in report['trials']], ['budget_stopped', 'unattempted'])
        self.assertFalse(report['trials'][0]['availability_failure'])
        self.assertEqual(report['summary'][0]['budget_stopped_trials'], 1)
        self.assertEqual({p.name for p in (root / report['trials'][1]['trial_id']).iterdir()}, {'trial.json'})
        with patch('tools.continue_conversation_study.PROJECT_ROOT', self.root), \
                patch('tools.continue_conversation_study.run') as launch, patch('builtins.print'):
            self.assertEqual(finish_untouched(root.name), 1)
            launch.assert_not_called()

    def test_continuation_uses_original_remaining_budget_and_keeps_failures(self):
        args = self.configure_run(2)
        with patch('payresolve.conversation_eval.PROJECT_ROOT', self.root), \
                patch('tools.continue_conversation_study.PROJECT_ROOT', self.root), \
                patch('payresolve.conversation_eval.fingerprint', return_value='frozen'), \
                patch('tools.continue_conversation_study.fingerprint', return_value='frozen'), \
                patch('payresolve.config.Settings.from_environment', return_value=self.settings()), \
                patch('payresolve.conversation_eval.subprocess.run', side_effect=self.worker), patch('builtins.print'):
            with patch('payresolve.conversation_eval.configured_planner',
                       side_effect=lambda *a, **k: self.fake_planner(error='gemini_service_unavailable')):
                run(args)
            source = next((self.root / 'instance/conversations').iterdir())
            original = (source / 'report.json').read_bytes()
            created = []
            with patch('payresolve.conversation_eval.configured_planner', side_effect=lambda *a, **k: self.fake_planner()):
                self.assertEqual(continue_run(source.name, created=created.append), 0)
            report = read(created[0] / 'report.json')
            self.assertEqual(report['request_budget']['owner_run_id'], source.name)
            self.assertEqual(report['budget_usage']['reserved_requests'], 2)
            self.assertEqual([r['status'] for r in report['trials']], ['failed', 'completed'])
            self.assertEqual((source / 'report.json').read_bytes(), original)
            self.assertFalse((created[0] / 'request-budget.db').exists())
            self.assertEqual(finish_untouched(created[0].name), 0)

    def test_budget_exhausted_between_trials_leaves_next_case_untouched(self):
        args = self.configure_run(1)
        with patch('payresolve.conversation_eval.PROJECT_ROOT', self.root), \
                patch('payresolve.conversation_eval.fingerprint', return_value='frozen'), \
                patch('payresolve.config.Settings.from_environment', return_value=self.settings()), \
                patch('payresolve.conversation_eval.configured_planner', side_effect=lambda *a, **k: self.fake_planner()), \
                patch('payresolve.conversation_eval.subprocess.run', side_effect=self.worker), patch('builtins.print'):
            self.assertEqual(run(args), 1)
        root = next((self.root / 'instance/conversations').iterdir())
        report = read(root / 'report.json')
        self.assertEqual([r['status'] for r in report['trials']], ['completed', 'unattempted'])
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['stop_reason'], 'evaluation_budget_exhausted')
        self.assertEqual(report['summary'][0]['budget_stopped_trials'], 0)
        self.assertEqual(report['budget_usage']['remaining'], 0)
