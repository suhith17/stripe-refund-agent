from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from payresolve.analysis import AnalysisService, AnalysisStore
from payresolve.config import Settings
from payresolve.fallback import FallbackExtractor
from payresolve.gemini_gateway import ExtractionError
from payresolve.m2 import execute
from tests.test_analysis import intent


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.primary = Mock(model="gemini-3.8-flash")
        self.primary.validate.side_effect = lambda message: message
        self.secondary = Mock(model="gemini-3.5-flash-lite")
        for extractor in (self.primary, self.secondary):
            extractor.extract.return_value = {"model": extractor.model, "intent": intent().model_dump(),
                                              "attempts": 1, "latency_ms": 10, "usage": {"total_tokens": 42}}
        self.extractor = FallbackExtractor(self.primary, self.secondary)

    def test_primary_success_never_calls_fallback(self):
        result = self.extractor.extract("Refund USD 5 from pi_1")
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["model"], self.primary.model)
        self.secondary.extract.assert_not_called()

    def test_availability_failure_uses_fallback_and_records_actual_model(self):
        for code in ("gemini_unavailable", "gemini_service_unavailable"):
            with self.subTest(code=code):
                self.primary.extract.side_effect = ExtractionError(code, "Unavailable.", attempts=3, latency_ms=100)
                result = self.extractor.extract("Refund USD 5 from pi_1")
                self.assertTrue(result["fallback_used"])
                self.assertEqual(result["model"], self.secondary.model)
                self.assertEqual(result["requested_model"], self.primary.model)
                self.assertEqual(result["attempts"], 4)
                self.assertEqual(result["attempt_history"][0]["error_code"], code)
                self.assertEqual(result["usage"]["total_tokens"], 42)

    def test_quota_auth_and_invalid_output_do_not_trigger_fallback(self):
        for code in ("gemini_rate_limited", "gemini_access_denied", "gemini_request_rejected",
                     "invalid_model_output", "gemini_incomplete", "credential_in_message"):
            with self.subTest(code=code):
                self.primary.extract.side_effect = ExtractionError(code, "Stopped.", attempts=1)
                with self.assertRaises(ExtractionError):
                    self.extractor.extract("Refund USD 5 from pi_1")
                self.secondary.extract.assert_not_called()

    def test_both_models_fail_once_and_history_survives_storage(self):
        self.primary.extract.side_effect = ExtractionError("gemini_service_unavailable", "Unavailable.", attempts=3)
        self.secondary.extract.side_effect = ExtractionError("gemini_service_unavailable", "Unavailable.", attempts=3)
        with TemporaryDirectory() as directory:
            store = AnalysisStore(Path(directory) / "test.db")
            policy = Mock()
            result = AnalysisService(store, self.extractor, policy).analyze(message="Refund USD 5 from pi_1")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["attempts"], 6)
            self.assertEqual(len(result["attempt_history"]), 2)
            self.assertEqual(store.analysis(result["id"]), result)
            policy.assess.assert_not_called()
        self.primary.extract.assert_called_once()
        self.secondary.extract.assert_called_once()

    def test_eval_ignores_configured_fallback(self):
        settings = Settings(Path("unused.db"), gemini_model="gemini-3.8-flash", gemini_fallback_model="gemini-3.5-flash-lite")
        args = SimpleNamespace(m2_command="eval", limit=2, interval=0)
        with patch("payresolve.m2.GeminiExtractor", return_value=self.primary) as factory, \
                patch("payresolve.m2.FallbackExtractor") as fallback, \
                patch("payresolve.evaluation.evaluate", return_value={}) as evaluate:
            execute(args, settings)
        factory.assert_called_once()
        fallback.assert_not_called()
        self.assertIs(evaluate.call_args.args[0], self.primary)

    def test_cli_analysis_uses_configured_fallback(self):
        with TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / "test.db", gemini_model="gemini-3.8-flash", gemini_fallback_model="gemini-3.5-flash-lite")
            args = SimpleNamespace(m2_command="analyze", message="Hello", request_id=None)
            self.primary.extract.side_effect = ExtractionError("gemini_service_unavailable", "Unavailable.", attempts=3)
            self.secondary.extract.return_value["intent"] = intent(intent="other").model_dump()
            with patch("payresolve.m2.GeminiExtractor", side_effect=[self.primary, self.secondary]):
                result = execute(args, settings)
            self.assertEqual(result["decision"]["action"], "no_action")
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["model"], "gemini-3.5-flash-lite")


if __name__ == "__main__":
    unittest.main()
