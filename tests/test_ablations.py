import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.ablations import FreshRetryKeyWorkflow, SkipFinalVerificationWorkflow, run_ablations
from payresolve.benchmark import DatasetError
from payresolve.recovery import load_suite, run_episode
from payresolve.workflow import SandboxWorkflow


class AblationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.episodes = {item.id: item for item in load_suite()[1]}

    def run_variant(self, episode_id, factory):
        return run_episode(self.episodes[episode_id], self.directory, workflow_factory=factory)

    def test_fresh_key_can_duplicate_partial_refund_but_keeps_commitment(self):
        result = self.run_variant("lost_partial", FreshRetryKeyWorkflow)
        self.assertEqual(result["duplicate_effects"], 1)
        self.assertEqual(result["workflow_effects"], 2)
        self.assertTrue(result["checks"]["journal_committed_before_posts"])
        self.assertFalse(result["checks"]["one_key_and_parameters"])
        self.assertEqual(result["local"]["approval"]["amount_minor"], 500)

    def test_provider_balance_blocks_duplicate_full_refund_but_recovery_fails(self):
        result = self.run_variant("lost_full", FreshRetryKeyWorkflow)
        self.assertEqual(result["duplicate_effects"], 0)
        self.assertEqual(result["workflow_effects"], 1)
        self.assertEqual(result["steps"][-1]["outcome"], "provider_invalid_refund")
        self.assertEqual(result["local"]["status"], "unresolved")
        self.assertFalse(result["passed"])

    def test_no_final_check_claims_success_while_pending(self):
        result = self.run_variant("pending_then_success", SkipFinalVerificationWorkflow)
        self.assertEqual(result["false_successes"], 1)
        self.assertEqual(result["steps"][0]["outcome"], "verified")
        self.assertEqual(result["steps"][0]["ledger"]["payments"][0]["amount_refunded"], 0)
        self.assertTrue(result["steps"][0]["local"]["result"]["verification_skipped"])

    def test_failed_refund_false_success_is_also_cached(self):
        result = self.run_variant("failed_is_terminal", SkipFinalVerificationWorkflow)
        self.assertEqual(result["false_successes"], 2)
        self.assertEqual(result["workflow_refund_posts"], 1)
        self.assertEqual(result["workflow_effects"], 0)

    def test_variants_refuse_nonlocal_provider_and_leave_normal_class_untouched(self):
        original_execute, original_verify = SandboxWorkflow._execute, SandboxWorkflow._verify
        for factory in (FreshRetryKeyWorkflow, SkipFinalVerificationWorkflow):
            with self.assertRaises(ValueError):
                factory(None, object())
        self.assertIs(SandboxWorkflow._execute, original_execute)
        self.assertIs(SandboxWorkflow._verify, original_verify)

    def test_matched_report_is_offline_and_preserves_both_arms(self):
        with patch("payresolve.config.Settings.from_environment", side_effect=AssertionError("no env")):
            result = run_ablations(pair_ids=["retry_partial"], output_directory=self.directory)
        self.assertEqual(result["baseline_passed"], 1)
        self.assertEqual(result["ablated_passed"], 0)
        self.assertEqual(result["completed_pairs"], 1)
        self.assertEqual((result["model_calls"], result["stripe_calls"]), (0, 0))
        saved = json.loads(Path(result["report_path"]).read_text())
        pair = saved["pairs"][0]
        self.assertEqual(pair["baseline"]["specification"], pair["ablated"]["specification"])
        before = set(self.directory.iterdir())
        with self.assertRaises(DatasetError):
            run_ablations(pair_ids=["absent"], output_directory=self.directory)
        self.assertEqual(set(self.directory.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
