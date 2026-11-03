from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from google import genai
from pydantic import ValidationError

from payresolve.__main__ import main
from payresolve.analysis import AnalysisService, AnalysisStore, DryRunPolicy
from payresolve.config import Settings
from payresolve.gemini_gateway import ExtractionError, GeminiExtractor
from payresolve.intent import Intent, amount_in_cents
from payresolve.evaluation import evaluate
from payresolve.workflow import SandboxWorkflow
from tests.test_workflow import FakeStripe


def intent(**overrides):
    values = dict(intent="refund", payment_ids=["pi_1"], order_id=None,
                  refund_type="partial", amount="5", amount_evidence="USD 5",
                  currency="usd", ambiguous=False, reason=None)
    return Intent(**(values | overrides))


class ContractTests(unittest.TestCase):
    def test_strict_schema(self):
        for change in ({"amount": 5.0}, {"ambiguous": "false"}, {"execute": True},
                       {"currency": "USD"}, {"amount": "0.001"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                intent(**change)

    def test_amount_requires_exact_single_numeric_evidence(self):
        for amount, evidence, message, expected in [
            ("5", "USD 5", "Refund USD 5", 500),
            ("0.29", "$0.29", "Refund $0.29", 29),
            ("1000.25", "$1,000.25", "Refund $1,000.25", 100025),
            ("100", "$1,00", "Refund $1,00", None),
            ("5", "$5", "Refund $7", None),
            ("5", "$7", "Refund $7", None),
            ("5", "$5 or $10", "Refund $5 or $10", None),
            ("5", "five dollars", "Refund five dollars", None),
            ("500", "500 cents", "Refund 500 cents", None),
        ]:
            with self.subTest(message=message):
                self.assertEqual(amount_in_cents(intent(amount=amount, amount_evidence=evidence), message), expected)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.extractor = GeminiExtractor("fake-gemini-key", "gemini-3.8-flash", forbidden_values=("fake-stripe-key",))

    def test_sdk_contract_is_stateless_and_has_no_tools(self):
        response = SimpleNamespace(status="completed", output_text=intent().model_dump_json(), usage=None)
        with patch("payresolve.gemini_gateway.genai.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.interactions.create.return_value = response
            result = self.extractor.extract("Refund USD 5 from pi_1")
        body = client.interactions.create.call_args.kwargs
        self.assertIs(body["store"], False)
        self.assertIs(body["automatic_continuation"], False)
        self.assertNotIn("tools", body)
        self.assertNotIn("fake-stripe-key", json.dumps(body))
        self.assertEqual(result["intent"]["amount"], "5")
        self.assertEqual(factory.call_args.kwargs["http_options"]["retry_options"]["attempts"], 2)

    def test_installed_sdk_serialization_and_response_parsing(self):
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={
                "status": "completed",
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": intent().model_dump_json()}]}],
                "usage": {"total_input_tokens": 12, "total_output_tokens": 30, "total_tokens": 42},
            })

        original = genai.Client

        def client(**options):
            options["http_options"]["client_args"]["transport"] = httpx.MockTransport(respond)
            return original(**options)

        with patch("payresolve.gemini_gateway.genai.Client", side_effect=client):
            result = self.extractor.extract("Refund USD 5 from pi_1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "POST")
        self.assertTrue(seen[0].url.path.endswith("/interactions"))
        body = json.loads(seen[0].content)
        self.assertIs(body["store"], False)
        self.assertNotIn("tools", body)
        self.assertEqual(result["intent"]["amount"], "5")
        self.assertEqual(result["usage"]["total_tokens"], 42)

    def test_credentials_never_reach_sdk(self):
        with patch("payresolve.gemini_gateway.genai.Client") as factory:
            for text in ("fake-gemini-key", "fake-stripe-key", "sk_live_example", "pk_test_example"):
                with self.subTest(text=text), self.assertRaises(ExtractionError):
                    self.extractor.extract(text)
            factory.assert_not_called()

    def test_invalid_incomplete_and_secret_responses_fail_closed(self):
        for status, output in [("completed", "not JSON"), ("in_progress", "{}"),
                               ("completed", intent(reason="fake-stripe-key").model_dump_json())]:
            with self.subTest(status=status), patch("payresolve.gemini_gateway.genai.Client") as factory:
                factory.return_value.__enter__.return_value.interactions.create.return_value = SimpleNamespace(
                    status=status, output_text=output)
                with patch("payresolve.gemini_gateway.time.sleep") as sleep:
                    with self.assertRaises(ExtractionError) as caught:
                        self.extractor.extract("Refund USD 5 from pi_1")
                    sleep.assert_not_called()
                factory.return_value.__enter__.return_value.interactions.create.assert_called_once()
                self.assertNotIn("fake-stripe-key", str(caught.exception))

    def test_errors_are_sanitized_and_not_retried(self):
        for status, code in [(429, "gemini_rate_limited"), (403, "gemini_access_denied"),
                             (400, "gemini_request_rejected"), (401, "gemini_access_denied"),
                             (404, "gemini_request_rejected"),
                             (None, "gemini_unavailable")]:
            error = Exception("secret upstream body fake-gemini-key")
            error.status_code = status
            with self.subTest(status=status), patch("payresolve.gemini_gateway.genai.Client") as factory:
                method = factory.return_value.__enter__.return_value.interactions.create
                method.side_effect = error
                with self.assertRaises(ExtractionError) as caught:
                    self.extractor.extract("Refund USD 5 from pi_1")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret upstream", str(caught.exception))
                method.assert_called_once()

    def test_transient_server_errors_retry_then_recover(self):
        original = genai.Client
        for status in (500, 502, 503, 504):
            seen = []

            def respond(request):
                seen.append(request)
                if len(seen) < 3:
                    return httpx.Response(status, json={"error": {"code": status, "message": "unavailable"}})
                return httpx.Response(200, json={"status": "completed", "steps": [
                    {"type": "model_output", "content": [{"type": "text", "text": intent().model_dump_json()}]}]})

            def client(**options):
                options["http_options"]["client_args"]["transport"] = httpx.MockTransport(respond)
                return original(**options)

            with self.subTest(status=status), patch("payresolve.gemini_gateway.genai.Client", side_effect=client), \
                    patch("payresolve.gemini_gateway.time.sleep") as sleep:
                result = self.extractor.extract("Refund USD 5 from pi_1")
            self.assertEqual(result["attempts"], 3)
            self.assertEqual(len(seen), 3)
            self.assertEqual(sleep.call_count, 2)
            for call, base in zip(sleep.call_args_list, (2, 4)):
                self.assertTrue(base <= call.args[0] <= base + 0.5)
            self.assertEqual(seen[0].content, seen[2].content)

    def test_real_sdk_retries_stop_at_three_and_sanitize_error(self):
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(503, json={"error": {"code": 503, "message": "private upstream details", "status": "UNAVAILABLE"}})

        original = genai.Client

        def client(**options):
            options["http_options"]["client_args"]["transport"] = httpx.MockTransport(respond)
            return original(**options)

        with patch("payresolve.gemini_gateway.genai.Client", side_effect=client), \
                patch("payresolve.gemini_gateway.time.sleep") as sleep:
            with self.assertRaises(ExtractionError) as caught:
                self.extractor.extract("Refund USD 5 from pi_1")
        self.assertEqual(len(seen), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(caught.exception.code, "gemini_service_unavailable")
        self.assertNotIn("private upstream", str(caught.exception))

    def test_nonretryable_errors_stop_http_retries(self):
        original = genai.Client
        for status, preceding in [(400, 0), (401, 0), (403, 0), (404, 0), (429, 0), (403, 1)]:
            seen = []

            def respond(request):
                seen.append(request)
                code = 503 if len(seen) <= preceding else status
                return httpx.Response(code, json={"error": {"code": code, "message": "private details"}})

            def client(**options):
                options["http_options"]["client_args"]["transport"] = httpx.MockTransport(respond)
                return original(**options)

            with self.subTest(status=status, preceding=preceding), \
                    patch("payresolve.gemini_gateway.genai.Client", side_effect=client), \
                    patch("payresolve.gemini_gateway.time.sleep") as sleep:
                with self.assertRaises(ExtractionError) as caught:
                    self.extractor.extract("Refund USD 5 from pi_1")
            self.assertEqual(len(seen), preceding + 1)
            self.assertEqual(sleep.call_count, preceding)
            self.assertEqual(caught.exception.attempts, preceding + 1)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.db"
        self.store = AnalysisStore(self.path)
        self.gateway = FakeStripe()
        self.workflow = SandboxWorkflow(self.store, self.gateway)
        self.workflow.create_payment("payment", 1000)
        self.reader = Mock(side_effect=self.gateway.get_payment)
        self.policy = DryRunPolicy(self.store, self.reader)

    def assess(self, text="Refund USD 5 from pi_1", **changes):
        return self.policy.assess(text, intent(**changes))

    def test_proposal_uses_fresh_balance_and_never_writes(self):
        self.gateway.payments["pi_1"].update(amount_refunded=700, refundable_amount=300)
        self.assertEqual(self.assess()["code"], "invalid_refund_amount")
        result = self.assess("Refund all remaining from pi_1", refund_type="full", amount=None, amount_evidence=None, currency=None)
        self.assertEqual(result["amount_minor"], 300)
        self.assertIs(result["executed"], False)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_missing_multiple_or_hallucinated_targets_never_retrieve(self):
        for text, ids in [("Refund USD 5", []), ("Refund USD 5 from pi_1", ["pi_2"]),
                          ("Refund USD 5 from pi_1 or pi_2", ["pi_1"])]:
            self.assertEqual(self.assess(text, payment_ids=ids)["action"], "clarify")
        self.reader.assert_not_called()

    def test_ambiguous_and_unspecified_requests_clarify(self):
        for change in ({"ambiguous": True}, {"intent": "unclear"}, {"refund_type": "unspecified"},
                       {"currency": None}, {"amount": None}, {"refund_type": "full"}):
            with self.subTest(change=change):
                self.assertEqual(self.assess(**change)["action"], "clarify")
        self.reader.assert_not_called()

    def test_invented_currency_is_not_accepted(self):
        result = self.assess("Refund 5 from pi_1", amount_evidence="5")
        self.assertEqual(result["code"], "specify_currency")
        self.reader.assert_not_called()

    def test_not_refund_and_invalid_amount_or_currency(self):
        self.assertEqual(self.assess(intent="other")["action"], "no_action")
        self.assertEqual(self.assess(currency="eur")["action"], "reject")
        for amount in ("0", "-5"):
            self.assertEqual(self.assess(f"Refund USD {amount} from pi_1", amount=amount,
                                         amount_evidence=f"USD {amount}")["action"], "reject")

    def test_ineligible_and_mismatched_payment_states(self):
        original = deepcopy(self.gateway.payments["pi_1"])
        for changes, code in [({"livemode": True}, "payment_identity_mismatch"),
                              ({"operation_key": "wrong"}, "payment_identity_mismatch"),
                              ({"id": "pi_wrong"}, "payment_identity_mismatch"),
                              ({"paid": False}, "payment_not_refundable"),
                              ({"captured": False}, "payment_not_refundable"),
                              ({"disputed": True}, "payment_disputed"),
                              ({"currency": "eur"}, "currency_mismatch"),
                              ({"refundable_amount": 0}, "invalid_refund_amount")]:
            with self.subTest(changes=changes):
                self.gateway.payments["pi_1"] = original | changes
                self.assertEqual(self.assess()["code"], code)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_unmanaged_and_pending_refund_block_read(self):
        self.assertEqual(self.assess("Refund USD 5 from pi_unknown", payment_ids=["pi_unknown"])["code"], "unmanaged_payment")
        self.workflow._prepare("pending", "refund", {"payment_id": "pi_1", "amount": 100, "currency": "usd"})
        self.assertEqual(self.assess()["code"], "unresolved_operation")
        self.reader.assert_not_called()

    def extractor(self):
        extractor = Mock(model="gemini-3.8-flash")
        extractor.validate.side_effect = lambda message: message
        extractor.extract.return_value = {"intent": intent().model_dump(), "usage": {}, "latency_ms": 1}
        return extractor

    def test_analysis_and_failure_survive_restart_without_changing_intake(self):
        extractor = self.extractor()
        service = AnalysisService(self.store, extractor, self.policy)
        first = service.analyze(message="Refund USD 5 from pi_1")
        self.assertEqual(first["decision"]["action"], "propose_refund")
        extractor.extract.side_effect = ExtractionError("gemini_rate_limited", "Quota reached.")
        second = service.analyze(request_id=first["request_id"])
        restarted = AnalysisStore(self.path)
        self.assertEqual(restarted.analysis(first["id"]), first)
        self.assertEqual(restarted.analysis(second["id"])["status"], "failed")
        self.assertEqual(restarted.get(first["request_id"])["status"], "received")
        self.assertEqual(self.gateway.refund_posts, [])

    def test_credential_message_is_not_saved(self):
        extractor = GeminiExtractor("fake-key", "gemini-3.8-flash")
        with self.assertRaises(ExtractionError):
            AnalysisService(self.store, extractor, self.policy).analyze(message="Refund with fake-key")
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM refund_requests").fetchone()[0], 0)

    def test_cli_analysis_and_show(self):
        settings = Settings(self.path)
        output = io.StringIO()
        with patch("sys.argv", ["payresolve", "m2", "analyze", "--message", "Refund USD 5 from pi_1"]), \
                patch("payresolve.__main__.Settings.from_environment", return_value=settings), \
                patch("payresolve.m2.GeminiExtractor", return_value=self.extractor()), \
                patch("payresolve.m2.StripeSandbox", return_value=self.gateway), redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        with patch("sys.argv", ["payresolve", "m2", "show", result["id"]]), \
                patch("payresolve.__main__.Settings.from_environment", return_value=settings), \
                patch("payresolve.m2.GeminiExtractor") as factory, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
            factory.assert_not_called()

    def test_eval_scores_labels_and_persists_provider_failure(self):
        extractor = self.extractor()
        extractor.extract.side_effect = [
            {"intent": intent(payment_ids=["pi_good"]).model_dump()},
            {"intent": intent(payment_ids=["pi_partial"], refund_type="full", amount=None,
                              amount_evidence=None, currency=None).model_dump()},
        ]
        result = evaluate(extractor, limit=2, interval=0, output_dir=self.path.parent / "evals")
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["stripe_calls"], 0)
        extractor.extract.side_effect = ExtractionError("gemini_rate_limited", "Quota reached.")
        result = evaluate(extractor, limit=2, interval=0, output_dir=self.path.parent / "evals")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["passed"], 0)
        saved = json.loads(Path(result["report_path"]).read_text())
        self.assertEqual(saved["cases"][0]["error"]["code"], "gemini_rate_limited")


if __name__ == "__main__":
    unittest.main()
