from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.benchmark import DatasetError
from payresolve.recovery import judge, load_suite, run_episode, run_suite
from payresolve.recovery_provider import RecoveryProvider
from payresolve.stripe_gateway import SandboxError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_twelve_workflow_episodes_meet_independent_expectations(self):
        _, episodes, _ = load_suite()
        self.assertEqual(len(episodes), 12)
        for episode in episodes:
            with self.subTest(episode=episode.id):
                directory = self.directory / episode.id
                directory.mkdir()
                with patch("payresolve.config.Settings.from_environment", side_effect=AssertionError("no env")):
                    result = run_episode(episode, directory)
                self.assertTrue(result["passed"], result["checks"])
                self.assertEqual(result["duplicate_effects"], 0)
                self.assertEqual(result["false_successes"], 0)

    def test_oracle_detects_false_verified_claim_without_effect(self):
        episode = load_suite()[1][0]
        result = run_episode(episode, self.directory)
        steps = deepcopy(result["steps"])
        steps[-1]["ledger"]["refunds"] = []
        verdict = judge(episode, steps, result["ledger"], result["local"], result["post_journal_checks"])
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["false_successes"], 1)

    def test_oracle_detects_duplicate_effect_and_excess_post(self):
        episode = load_suite()[1][0]
        result = run_episode(episode, self.directory)
        ledger = deepcopy(result["ledger"])
        ledger["refunds"].append(ledger["refunds"][0] | {"id": "re_duplicate"})
        ledger["posts"].append(ledger["posts"][0])
        verdict = judge(episode, result["steps"], ledger, result["local"], result["post_journal_checks"])
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["duplicate_effects"], 1)
        self.assertFalse(verdict["checks"]["expected_post_count"])

    def provider(self):
        provider = RecoveryProvider(self.directory / "provider.db")
        provider.create_payment({"amount": 1000, "currency": "usd", "confirm": True}, "fixture")
        return provider

    def test_provider_lost_response_commits_effect_and_reuses_key(self):
        provider = self.provider()
        params = dict(payment_intent="pi_recovery", amount=500, metadata={"payresolve_operation": "key"})
        provider.fail_once("after_refund")
        with self.assertRaises(SandboxError):
            provider.create_refund(params, "key")
        self.assertEqual(provider.snapshot()["payments"][0]["amount_refunded"], 500)
        provider.create_refund(params, "key")
        self.assertEqual(len(provider.snapshot()["refunds"]), 1)
        with self.assertRaises(SandboxError) as caught:
            provider.create_refund(params | {"amount": 600}, "key")
        self.assertEqual(caught.exception.code, "provider_idempotency_conflict")

    def test_pending_reserves_capacity_but_only_settlement_changes_balance(self):
        provider = self.provider()
        params = dict(payment_intent="pi_recovery", amount=700, metadata={"payresolve_operation": "key"})
        provider.next_refund_status = "pending"
        provider.create_refund(params, "key")
        self.assertEqual(provider.snapshot()["payments"][0]["amount_refunded"], 0)
        with self.assertRaises(SandboxError):
            provider.create_refund(params | {"amount": 400}, "other")
        provider.settle_pending()
        self.assertEqual(provider.snapshot()["payments"][0]["amount_refunded"], 700)

    def test_report_contains_evidence_and_invalid_selection_creates_no_run(self):
        output = self.directory / "reports"
        result = run_suite(case_ids=["expired_before_effect"], output_directory=output)
        self.assertEqual((result["scheduled"], result["passed"], result["model_calls"], result["stripe_calls"]), (1, 1, 0, 0))
        saved = json.loads(Path(result["report_path"]).read_text())
        self.assertEqual(saved["episodes"][0]["steps"][-1]["outcome"], "reconciliation_required")
        before = set(output.iterdir())
        with self.assertRaises(DatasetError):
            run_suite(case_ids=["absent"], output_directory=output)
        self.assertEqual(set(output.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
