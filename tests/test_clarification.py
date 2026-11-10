from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from payresolve.__main__ import main
from payresolve.analysis import AnalysisService, AnalysisStore, DryRunPolicy
from payresolve.config import Settings
from payresolve.execution import ExecutionStore
from payresolve.gemini_gateway import ExtractionError, validate_message
from payresolve.stripe_gateway import SandboxError
from payresolve.workflow import SandboxWorkflow
from tests.test_analysis import intent
from tests.test_workflow import FakeStripe


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.db"
        self.store = AnalysisStore(self.path)
        self.gateway = FakeStripe()
        SandboxWorkflow(self.store, self.gateway).create_payment("fixture", 1000)
        self.extractor = Mock(model="gemini-3.5-flash-lite")
        self.extractor.validate.side_effect = lambda text: validate_message(text, ("fake-secret",))
        self.policy = DryRunPolicy(self.store, self.gateway.get_payment)
        self.service = AnalysisService(self.store, self.extractor, self.policy)
        self.set_intent(currency=None, amount_evidence="5")
        self.parent = self.service.analyze(message="Refund 5 from pi_1")

    def set_intent(self, **changes):
        self.extractor.extract.side_effect = None
        self.extractor.extract.return_value = {"intent": intent(**changes).model_dump(),
                                              "attempts": 1, "latency_ms": 1, "usage": {}}

    def clarify(self, parent=None):
        self.set_intent()
        return self.service.clarify((parent or self.parent)["id"], "Refund USD 5 from pi_1")

    def test_targeted_question_and_response_contract(self):
        clarification = self.parent["decision"]["clarification"]
        self.assertIn("currency", clarification["question"])
        self.assertEqual(clarification["response_format"], "complete_refund_request")

    def test_complete_response_preserves_request_and_parent_across_restart(self):
        result = self.clarify()
        self.assertEqual(result["decision"]["action"], "propose_refund")
        self.assertEqual(result["request_id"], self.parent["request_id"])
        self.assertEqual(result["parent_analysis_id"], self.parent["id"])
        self.assertEqual(result["input_message"], "Refund USD 5 from pi_1")
        restarted = AnalysisStore(self.path)
        history = restarted.history(result["request_id"])
        self.assertEqual(history["request"]["message"], "Refund 5 from pi_1")
        self.assertEqual(history["request"]["status"], "received")
        self.assertEqual(history["analyses"], [self.parent, result])
        self.extractor.extract.assert_called_with("Refund USD 5 from pi_1")
        self.assertEqual(self.gateway.refund_posts, [])

    def test_partial_response_is_not_silently_merged(self):
        self.set_intent(intent="unclear", payment_ids=[], amount=None, amount_evidence=None)
        second = self.service.clarify(self.parent["id"], "USD")
        self.assertEqual(second["decision"]["action"], "clarify")
        self.extractor.extract.assert_called_with("USD")
        third = self.clarify(second)
        self.assertEqual(third["decision"]["action"], "propose_refund")
        self.assertEqual(len(self.store.history(third["request_id"])["analyses"]), 3)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_nonclarification_or_unknown_parent_is_rejected_before_model_call(self):
        for action in ("propose_refund", "reject", "no_action"):
            parent = dict(self.parent, decision={"action": action})
            self.store.finish_analysis(parent)
            self.extractor.extract.reset_mock()
            with self.subTest(action=action), self.assertRaises(ExtractionError):
                self.service.clarify(parent["id"], "Refund USD 5 from pi_1")
            self.extractor.extract.assert_not_called()
        self.extractor.extract.reset_mock()
        with self.assertRaises(ExtractionError):
            self.service.clarify("unknown", "Refund USD 5 from pi_1")
        self.extractor.extract.assert_not_called()

    def test_secret_or_empty_response_is_not_saved_or_sent(self):
        self.extractor.extract.reset_mock()
        for text in ("fake-secret", "", "sk_test_example", "x" * 4001):
            with self.subTest(text=text[:20]), self.assertRaises(ExtractionError):
                self.service.clarify(self.parent["id"], text)
        self.extractor.extract.assert_not_called()
        self.assertEqual(len(self.store.history(self.parent["request_id"])["analyses"]), 1)

    def test_provider_failure_preserves_input_and_can_retry_original_parent(self):
        self.extractor.extract.side_effect = ExtractionError("gemini_rate_limited", "Quota reached.", attempts=1)
        failed = self.service.clarify(self.parent["id"], "Refund USD 5 from pi_1")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["input_message"], "Refund USD 5 from pi_1")
        self.assertEqual(failed["parent_analysis_id"], self.parent["id"])
        result = self.clarify()
        self.assertEqual(result["decision"]["action"], "propose_refund")
        self.assertEqual(len(self.store.history(result["request_id"])["analyses"]), 3)

    def test_clarification_cannot_bypass_one_approval_per_request(self):
        first, second = self.clarify(), self.clarify()
        approvals = ExecutionStore(self.path)
        with self.assertRaises(SandboxError):
            approvals.approve(self.parent["id"], "pi_1", 500, "usd")
        approvals.approve(first["id"], "pi_1", 500, "usd")
        with self.assertRaises(SandboxError) as caught:
            approvals.approve(second["id"], "pi_1", 500, "usd")
        self.assertEqual(caught.exception.code, "request_already_approved")
        self.extractor.extract.reset_mock()
        with self.assertRaises(ExtractionError) as caught:
            self.clarify()
        self.assertEqual(caught.exception.code, "request_already_approved")
        self.extractor.extract.assert_not_called()

    def test_legacy_analysis_without_input_message_can_be_clarified(self):
        self.parent.pop("input_message")
        self.store.finish_analysis(self.parent)
        result = self.clarify()
        self.assertEqual(result["decision"]["action"], "propose_refund")
        self.assertEqual(self.store.history(result["request_id"])["request"]["message"], "Refund 5 from pi_1")

    def test_cli_clarify_and_local_history(self):
        self.set_intent()
        output = io.StringIO()
        settings = Settings(self.path)
        with patch("sys.argv", ["payresolve", "m2", "clarify", self.parent["id"], "--message", "Refund USD 5 from pi_1"]), \
                patch("payresolve.__main__.Settings.from_environment", return_value=settings), \
                patch("payresolve.m2.GeminiExtractor", return_value=self.extractor), \
                patch("payresolve.m2.StripeSandbox", return_value=self.gateway), redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["request_id"], self.parent["request_id"])
        output = io.StringIO()
        with patch("sys.argv", ["payresolve", "m2", "history", result["request_id"]]), \
                patch("payresolve.__main__.Settings.from_environment", return_value=settings), \
                patch("payresolve.m2.GeminiExtractor") as model, \
                patch("payresolve.m2.StripeSandbox") as gateway, redirect_stdout(output):
            self.assertEqual(main(), 0)
            model.assert_not_called()
            gateway.assert_not_called()
        self.assertEqual(len(json.loads(output.getvalue())["analyses"]), 2)


if __name__ == "__main__":
    unittest.main()
