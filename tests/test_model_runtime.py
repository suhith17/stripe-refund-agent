from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest

from payresolve.agent import GeminiPlanner
from payresolve.config import Settings
from payresolve.gemini_gateway import ExtractionError
from payresolve.model_runtime import RequestPacer, ChatPlanner, configured_planner


class Clock:
    now = 1000.

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'pacing.db'
        self.clock = Clock()

    def pacer(self):
        return RequestPacer(self.path, 15, clock=self.clock.read, monotonic=self.clock.read, sleep=self.clock.sleep)

    def test_shared_instances_space_all_models_and_window_has_at_most_15_starts(self):
        times = []
        for index in range(31):
            self.pacer().wait('gemini-a' if index % 2 else 'gemini-b', self.clock.now + 120)
            times.append(self.clock.now)
        self.assertTrue(all(b - a >= 4 for a, b in zip(times, times[1:])))
        self.assertTrue(all(sum(start <= t < start + 60 for t in times) <= 15 for start in times))

    def test_deadline_failure_does_not_reserve_future_slots(self):
        pacer = self.pacer()
        pacer.wait('gemini-a', self.clock.now + 120)
        with self.assertRaises(ExtractionError) as caught:
            pacer.wait('gemini-a', self.clock.now + 1)
        self.assertEqual(caught.exception.code, 'gemini_pacing_timeout')
        self.assertEqual(self.clock.now, 1000.)
        pacer.wait('gemini-a', self.clock.now + 120)
        self.assertAlmostEqual(self.clock.now, 1004.05)

    def test_cooldown_survives_reopening_and_other_model_can_proceed(self):
        self.pacer().cooldown('gemini-a', 90)
        with self.assertRaises(ExtractionError) as caught:
            self.pacer().wait('gemini-a', self.clock.now + 120)
        self.assertEqual(caught.exception.code, 'gemini_rate_limited')
        self.pacer().wait('gemini-b', self.clock.now + 120)
        self.clock.sleep(91)
        self.pacer().wait('gemini-a', self.clock.now + 120)

    def test_one_switch_keeps_context_and_tracks_failed_attempt_without_hiding_unknown_tokens(self):
        primary, backup = Mock(model='gemini-a'), Mock(model='gemini-b')
        primary.step.side_effect = ExtractionError('gemini_service_unavailable', 'Unavailable', attempts=1)
        backup.step.return_value = {'action': {'action': 'ask', 'reason': 'purchase'}, 'attempts': 1,
                                  'usage': {'total_tokens': 8}, 'latency_ms': 1}
        planner = ChatPlanner(primary, backup)
        context = {'messages': [{'content': 'refund'}]}
        result = planner.step(context, remaining_seconds=10)
        self.assertEqual(result['model'], 'gemini-b')
        self.assertEqual(result['attempts'], 2)
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['model_attempts'][0]['usage'], None)
        self.assertIs(backup.step.call_args.args[0], context)
        planner.step(context, remaining_seconds=10)
        self.assertEqual(primary.step.call_count, 1)
        self.assertEqual(backup.step.call_count, 2)

    def test_invalid_output_access_errors_and_local_deadlines_never_trigger_backup(self):
        for code in ('invalid_model_output', 'gemini_access_denied', 'gemini_request_rejected', 'gemini_pacing_timeout'):
            with self.subTest(code=code):
                primary, backup = Mock(model='gemini-a'), Mock(model='gemini-b')
                primary.step.side_effect = ExtractionError(code, 'Failure', attempts=1)
                with self.assertRaises(ExtractionError):
                    ChatPlanner(primary, backup).step({}, remaining_seconds=10)
                backup.step.assert_not_called()

    def test_backup_failure_is_bounded_and_records_both_models(self):
        primary, backup = Mock(model='gemini-a'), Mock(model='gemini-b')
        for planner in (primary, backup):
            planner.step.side_effect = ExtractionError('gemini_rate_limited', 'Quota', attempts=1)
        with self.assertRaises(ExtractionError) as caught:
            ChatPlanner(primary, backup).step({}, remaining_seconds=10)
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(len(caught.exception.model_attempts), 2)
        self.assertEqual(primary.step.call_count, 1)
        self.assertEqual(backup.step.call_count, 1)

    def test_evaluation_factory_ignores_configured_backup(self):
        settings = Settings(self.path.parent / 'cases.db', agent_fallback_model='gemini-b', model_pacing_path=self.path)
        planner = configured_planner(settings, GeminiPlanner, evaluation=True)
        self.assertIsInstance(planner, GeminiPlanner)
        self.assertEqual(planner.model, settings.gemini_model)


if __name__ == '__main__':
    unittest.main()
