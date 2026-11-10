from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from payresolve import benchmark_runner as runner
from payresolve.benchmark import DatasetError, parse_dataset
from payresolve.benchmark_models import GeminiDecisionWrapper, WrapperOutput
from payresolve.config import PROJECT_ROOT, Settings
from payresolve.gemini_gateway import ExtractionError

PILOT = PROJECT_ROOT / "evals" / "benchmark" / "pilot.json"
MODEL = "gemini-3.5-flash-lite"


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.case = parse_dataset(json.loads(PILOT.read_text())).cases[0]
        expected = self.case.initial.expected_decision
        self.output = {"intent": self.case.initial.reference_intent.model_dump(), "decision": {
            "action": expected.action, "code": expected.reason_codes[0], "payment_id": expected.payment_id,
            "amount_minor": expected.amount_minor, "currency": expected.currency}}

    def test_schema_checks_shape_but_does_not_apply_policy(self):
        self.output["decision"]["amount_minor"] = 99999
        self.assertEqual(WrapperOutput.model_validate(self.output).decision.amount_minor, 99999)
        for value in (True, 2.5, None, 0):
            self.output["decision"]["amount_minor"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                WrapperOutput.model_validate(self.output)

    def test_prompt_contains_only_message_and_fixtures_and_shared_transport_options(self):
        adapter = GeminiDecisionWrapper("unit-test-key", MODEL)
        response = SimpleNamespace(status="completed", output_text=json.dumps(self.output), usage=None)
        with patch("payresolve.gemini_gateway.genai.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.interactions.create.return_value = response
            result = adapter.decide(self.case.initial.message, self.case.payments)
        sent = client.interactions.create.call_args.kwargs
        payload = json.loads(sent["input"])
        self.assertEqual(set(payload), {"message", "payments"})
        self.assertNotIn(self.case.rationale, sent["input"])
        self.assertNotIn("expected_decision", sent["input"])
        self.assertFalse(sent["store"])
        self.assertFalse(sent["automatic_continuation"])
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["generation_config"], {"thinking_level": "low", "max_output_tokens": 2048})
        self.assertEqual(result["output"], self.output)

    def test_invalid_output_and_secret_echo_are_sanitized(self):
        adapter = GeminiDecisionWrapper("unit-test-key", MODEL)
        for body in ("not json", json.dumps(self.output).replace('"reason": null', '"reason": "unit-test-key"')):
            with patch.object(adapter, "_request", return_value=(SimpleNamespace(status="completed", output_text=body), 1)), \
                    self.assertRaises(ExtractionError) as caught:
                adapter.decide(self.case.initial.message, self.case.payments)
            self.assertEqual(caught.exception.code, "invalid_model_output")
            self.assertNotIn("unit-test-key", str(caught.exception))


class ModelRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.dataset = parse_dataset(json.loads(PILOT.read_text()))
        self.settings = Settings(self.directory / "unused.db", gemini_api_key="unit-test-key")
        settings_patch = patch.object(runner.Settings, "from_environment", return_value=self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)

    def extraction(self, turn):
        return {"intent": turn.reference_intent.model_dump(), "model": MODEL, "prompt_version": "test",
                "attempts": 1, "latency_ms": 10, "usage": {"total_tokens": 100}}

    def start(self, arm="full", case_ids=None, model=MODEL):
        return runner.create_run(PILOT, arm=arm, model=model if arm != "rules" else None,
                                 case_ids=case_ids or [self.dataset.cases[0].id], directory=self.directory)

    def fake(self, turns):
        adapter = Mock(model=MODEL)
        adapter.extract.side_effect = [self.extraction(turn) for turn in turns]
        return adapter

    def test_full_uses_saved_model_and_policy_with_usage(self):
        case = deepcopy(self.dataset.cases[0])
        fake = self.fake([case.initial])
        run_id = self.start()
        with patch.object(runner, "GeminiExtractor", return_value=fake) as factory:
            result = runner.resume(run_id, directory=self.directory)
        self.assertEqual(factory.call_args.args[1], MODEL)
        fake.extract.assert_called_once_with(case.initial.message)
        self.assertEqual((result["passed"], result["model_calls"], result["http_attempts_known"]), (1, 1, 1))
        self.assertEqual(result["reported_total_tokens"], 100)
        self.assertFalse(result["fallback_enabled"])
        self.assertEqual(result["stripe_calls"], 0)

    def test_wrapper_wrong_proposal_is_graded_not_corrected_by_policy(self):
        case = self.dataset.cases[0]
        fake = Mock(model=MODEL)
        fake.decide.return_value = self.extraction(case.initial) | {"output": {
            "intent": case.initial.reference_intent.model_dump(), "decision": {
                "action": "propose_refund", "code": "eligible_at_analysis_time", "payment_id": case.payments[0].id,
                "amount_minor": 99999, "currency": "usd"}}}
        run_id = self.start(arm="wrapper")
        with patch.object(runner, "GeminiDecisionWrapper", return_value=fake), \
                patch.object(runner, "fixture_policy", side_effect=AssertionError("wrapper must bypass policy")):
            result = runner.resume(run_id, directory=self.directory)
        self.assertEqual((result["completed"], result["passed"]), (1, 0))
        row = runner.report(run_id, directory=self.directory, details=True)["cases"][0]
        self.assertEqual(row["turns"][0]["decision"]["amount_minor"], 99999)
        self.assertFalse(row["turns"][0]["decision"]["executed"])

    def test_provider_error_stops_batch_and_preserves_prior_clarification_turn(self):
        case = self.dataset.cases[2]
        fake = self.fake([case.initial])
        fake.extract.side_effect = [self.extraction(case.initial),
                                   ExtractionError("gemini_rate_limited", "sanitized", attempts=1, latency_ms=20)]
        run_id = self.start(case_ids=[case.id, self.dataset.cases[3].id])
        with patch.object(runner, "GeminiExtractor", return_value=fake):
            result = runner.resume(run_id, limit=2, directory=self.directory)
        self.assertEqual((result["errors"], result["unattempted"], result["model_calls"]), (1, 1, 2))
        self.assertEqual(result["reported_total_tokens"], 100)
        self.assertEqual(result["calls_with_unknown_token_usage"], 1)
        row = runner.report(run_id, directory=self.directory, details=True)["cases"][0]
        self.assertEqual(len(row["turns"]), 1)
        self.assertEqual(row["error"]["code"], "gemini_rate_limited")

    def test_interrupted_call_stays_unknown_and_is_not_repeated(self):
        case = self.dataset.cases[2]
        fake = self.fake([case.initial])
        fake.extract.side_effect = [self.extraction(case.initial), KeyboardInterrupt()]
        run_id = self.start(case_ids=[case.id])
        with patch.object(runner, "GeminiExtractor", return_value=fake), self.assertRaises(KeyboardInterrupt):
            runner.resume(run_id, directory=self.directory)
        result = runner.report(run_id, directory=self.directory, details=True)
        self.assertEqual(result["in_progress"], 1)
        self.assertEqual(result["calls_with_unknown_attempts"], 1)
        self.assertEqual(len(result["cases"][0]["turns"]), 1)
        with patch.object(runner, "GeminiExtractor", side_effect=AssertionError("no replay")):
            self.assertEqual(runner.resume(run_id, directory=self.directory)["in_progress"], 1)

    def test_comparison_rejects_mismatched_models_selections_and_arms(self):
        rules = self.start(arm="rules")
        full = self.start()
        comparison = runner.compare([rules, full], directory=self.directory)
        self.assertEqual(comparison["status"], "incomplete")
        other = self.start(arm="wrapper", model="gemini-3.8-flash")
        with self.assertRaises(DatasetError):
            runner.compare([full, other], directory=self.directory)
        with self.assertRaises(DatasetError):
            runner.compare([rules, self.start(arm="rules")], directory=self.directory)
        other = self.start(arm="wrapper", case_ids=[self.dataset.cases[1].id])
        with self.assertRaises(DatasetError):
            runner.compare([full, other], directory=self.directory)


if __name__ == "__main__":
    unittest.main()
