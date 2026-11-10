from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from unittest.mock import patch

from tools.continue_conversation_study import untouched_trials, finish_untouched


class ContinuationTests(unittest.TestCase):
    def test_expanded_export_keeps_missing_arms_and_models_separate(self):
        from tools.summarize_expanded_study import counters, paired
        def row(model, mode, status, success, **extra):
            return {'model':model,'scenario_id':'same-id','context_mode':mode,'status':status,
                    'success':success,'checks':[],'model_tokens':None,'model_latency_ms':None,**extra}
        rows = [row('primary','history','completed',True,model_tokens=20,model_latency_ms=40),
                row('primary','structured','unattempted',False),
                row('backup','history','failed',False,availability_failure=True),
                row('backup','structured','completed',True,model_tokens=40,model_latency_ms=60)]
        summary = counters(rows)
        self.assertEqual((summary['scheduled'],summary['attempted'],summary['passed']), (4,3,2))
        self.assertEqual(summary['success_rate_scheduled'], .5)
        self.assertEqual(summary['success_rate_attempted'], 2/3)
        self.assertEqual(summary['mean_model_tokens_known_trials'], 30)
        self.assertEqual(summary['token_eligible_trials'], 2)
        self.assertEqual(summary['provider_failures'], 1)
        self.assertEqual(summary['behavior_or_oracle_failures'], 0)
        comparison = paired(rows)
        self.assertEqual(comparison['both_attempted'], 1)
        self.assertEqual(comparison['missing_arm'], 1)
        self.assertEqual(comparison['structured_only_pass'], 1)
        self.assertEqual(comparison['pairs_with_infrastructure_or_budget_issue'], 1)

    def test_preserves_order_and_skips_all_attempted_trials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'trial_id': name, 'status': status} for name, status in
                    (('failed', 'failed'), ('active', 'running'), ('passed', 'completed'),
                     ('second', 'unattempted'), ('first', 'unattempted'))]
            for name in ('first', 'second'):
                (root / name).mkdir()
                (root / name / 'trial.json').write_text('{}')
            self.assertEqual([p.name for p in untouched_trials(root, {'trials': rows})], ['second', 'first'])

    def test_refuses_artifacts_created_before_a_state_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'trial').mkdir()
            (root / 'trial/trial.json').write_text('{}')
            (root / 'trial/calls.json').write_text('[]')
            with self.assertRaises(ValueError):
                untouched_trials(root, {'trials': [{'trial_id': 'trial', 'status': 'unattempted'}]})

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            untouched_trials(Path('.'), {'trials': [{'trial_id': '../foreign', 'status': 'unattempted'}]})

    def test_automatic_collection_stops_on_quota_without_starting_another_segment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_id = 'a' * 32
            path = root / 'instance/conversations' / source_id
            path.mkdir(parents=True)
            (path / 'report.json').write_text(json.dumps({'trials': [
                {'status': 'failed', 'source_run_id': source_id, 'calls': [{'status': 'gemini_rate_limited'}]},
                {'status': 'unattempted'}]}))
            with patch('tools.continue_conversation_study.PROJECT_ROOT', root), \
                    patch('tools.continue_conversation_study.run') as start, patch('builtins.print'):
                self.assertEqual(finish_untouched(source_id), 1)
                start.assert_not_called()

    def test_automatic_collection_keeps_failure_and_advances_only_to_new_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_id, next_id = 'a' * 32, 'b' * 32
            source = root / 'instance/conversations' / source_id
            source.mkdir(parents=True)
            original = {'trials': [
                {'status': 'failed', 'source_run_id': source_id, 'calls': [{'status': 'gemini_service_unavailable'}]},
                {'status': 'unattempted'}]}
            raw = json.dumps(original)
            (source / 'report.json').write_text(raw)
            def start(sid, *, created):
                self.assertEqual(sid, source_id)
                target = source.parent / next_id
                target.mkdir()
                (target / 'report.json').write_text(json.dumps({'trials': [original['trials'][0], {'status': 'completed'}]}))
                created(target)
                return 0
            with patch('tools.continue_conversation_study.PROJECT_ROOT', root), \
                    patch('tools.continue_conversation_study.run', side_effect=start) as launch, \
                    patch('tools.continue_conversation_study.time.sleep') as pause, patch('builtins.print'):
                self.assertEqual(finish_untouched(source_id), 0)
                launch.assert_called_once()
                pause.assert_called_once()
            self.assertEqual((source / 'report.json').read_text(), raw)
