from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve import create_app
from payresolve.config import Settings
from payresolve.execution import ExecutionStore
from payresolve.gemini_gateway import ExtractionError, validate_message
from payresolve.rules import extract
from payresolve.workflow import SandboxWorkflow
from tests.test_workflow import FakeStripe


class LocalExtractor:
    def __init__(self, api_key, model, *, forbidden_values=()):
        self.model = model
        self.secrets = (api_key, *forbidden_values)

    def validate(self, message):
        return validate_message(message, self.secrets)

    def extract(self, message):
        return {"intent": extract(self.validate(message)).model_dump(), "model": self.model,
                "attempts": 1, "latency_ms": 1, "prompt_version": "offline-test"}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name) / "app.db", stripe_secret_key="sk_test_example", gemini_api_key="test-model-secret")
        self.store = ExecutionStore(self.settings.database_path)
        self.gateway = FakeStripe()
        SandboxWorkflow(self.store, self.gateway).create_payment("fixture", 1000)
        self.app = create_app(self.settings)
        self.client = self.app.test_client()
        for target, replacement in (("payresolve.m2.GeminiExtractor", LocalExtractor), ("payresolve.m2.StripeSandbox", lambda key: self.gateway)):
            mocked = patch(target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client.get("/")

    def post(self, url, **data):
        with self.client.session_transaction() as session:
            data["csrf"] = session["csrf"]
        return self.client.post(url, data=data)

    def create(self, message="Refund USD 5 from pi_1"):
        response = self.post("/ui/requests", message=message)
        self.assertEqual(response.status_code, 303)
        request_id = response.headers["Location"].rsplit("/", 1)[-1]
        return self.store.history(request_id)["analyses"][-1]

    def act(self, run, action, **data):
        return self.post(f"/ui/analyses/{run['id']}/{action}", **data)

    def test_home_and_detail_are_local_reads(self):
        run = self.create()
        with patch("payresolve.m2.execute", side_effect=AssertionError("GET must not call APIs")):
            home = self.client.get("/")
            detail = self.client.get(f"/ui/requests/{run['request_id']}")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Ready for review", detail.data)
        self.assertIn(b"$5.00", detail.data)
        self.assertEqual(home.headers["Cache-Control"], "no-store")
        self.assertEqual(home.headers["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", home.headers["Content-Security-Policy"])

    def test_request_clarification_approval_execution_and_verify(self):
        parent = self.create("Refund 5 from pi_1")
        self.assertEqual(parent["decision"]["action"], "clarify")
        response = self.act(parent, "clarify", message="Refund USD 5 from pi_1")
        self.assertEqual(response.status_code, 303)
        child = self.store.history(parent["request_id"])["analyses"][-1]
        self.assertEqual(child["parent_analysis_id"], parent["id"])
        self.assertEqual(self.act(child, "execute").status_code, 409)
        self.assertEqual(self.act(child, "approve", confirm="yes", payment_id="pi_1", amount="600", currency="usd").status_code, 409)
        self.assertEqual(self.act(child, "approve", confirm="yes", payment_id="pi_1", amount="500", currency="usd").status_code, 303)
        self.assertEqual(len(self.gateway.refund_posts), 0)
        self.assertEqual(self.act(child, "execute").status_code, 303)
        self.assertEqual(self.act(child, "execute").status_code, 303)
        self.assertEqual(self.act(child, "verify").status_code, 303)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        page = self.client.get(f"/ui/requests/{child['request_id']}")
        self.assertIn(b"confirmed this refund", page.data)
        self.assertNotIn(b"Save approval", page.data)

    def test_post_guards_and_get_cannot_execute(self):
        run = self.create()
        path = f"/ui/analyses/{run['id']}/execute"
        self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(self.client.post(path).status_code, 403)
        self.assertEqual(self.client.post(path, data={"csrf": "é"}).status_code, 403)
        with self.client.session_transaction() as session:
            token = session["csrf"]
        self.assertEqual(self.client.post(path, data={"csrf": token}, headers={"Origin": "https://outside.example"}).status_code, 403)
        self.assertEqual(self.client.get("/", environ_overrides={"REMOTE_ADDR": "203.0.113.7"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "outside.example"}).status_code, 400)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_browser_origin_and_stale_session_approval_guards(self):
        run = self.create()
        path = f"/ui/analyses/{run['id']}/approve"
        with self.client.session_transaction() as session:
            token = session['csrf']
        data = dict(csrf=token, confirm='yes', payment_id='pi_1', amount='500', currency='usd')
        for origin in ('null', 'https://outside.example', 'http://localhost:9999'):
            response = self.client.post(path, data=data, headers={'Origin': origin})
            self.assertEqual(response.status_code, 403)
            self.assertIn(b'form_origin_rejected', response.data)
        response = self.client.post(path, data={**data, 'csrf':'stale'}, headers={'Origin':'http://localhost'})
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'form_session_expired', response.data)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM refund_approvals').fetchone()[0], 0)
        response = self.client.post(path, data=data, headers={'Origin':'http://localhost'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_input_is_escaped_and_old_secrets_are_redacted(self):
        record = self.store.create('<script>alert("x")</script> sk_test_example test-model-secret')
        for path in ("/", f"/ui/requests/{record['id']}"):
            response = self.client.get(path)
            self.assertNotIn(b"<script>alert", response.data)
            self.assertNotIn(b"sk_test_example", response.data)
            self.assertNotIn(b"test-model-secret", response.data)
            self.assertIn(b"[redacted]", response.data)

    def test_credentials_rejected_before_new_request_is_saved(self):
        with self.store.connect() as connection:
            before = connection.execute("SELECT count(*) FROM refund_requests").fetchone()[0]
        response = self.post("/ui/requests", message="Please refund test-model-secret")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"test-model-secret", response.data)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM refund_requests").fetchone()[0], before)

    def test_model_failure_is_visible_and_retry_preserves_history(self):
        with patch.object(LocalExtractor, "extract", side_effect=ExtractionError("gemini_unavailable", "Provider unavailable.")):
            failed = self.create()
        page = self.client.get(f"/ui/requests/{failed['request_id']}")
        self.assertIn(b"Provider unavailable", page.data)
        self.assertIn(b"Retry analysis", page.data)
        self.assertEqual(self.act(failed, "retry").status_code, 303)
        history = self.store.history(failed["request_id"])
        self.assertEqual(len(history["analyses"]), 2)
        self.assertEqual(history["analyses"][0]["status"], "failed")

    def test_failed_clarification_retry_uses_corrected_text(self):
        parent = self.create("Refund 5 from pi_1")
        with patch.object(LocalExtractor, "extract", side_effect=ExtractionError("gemini_unavailable", "Provider unavailable.")):
            self.act(parent, "clarify", message="Refund USD 3 from pi_1")
        failed = self.store.history(parent["request_id"])["analyses"][-1]
        self.assertEqual(self.act(failed, "retry").status_code, 303)
        retried = self.store.history(parent["request_id"])["analyses"][-1]
        self.assertEqual(retried["decision"]["amount_minor"], 300)
        self.assertEqual(retried["parent_analysis_id"], parent["id"])

    def test_existing_intake_can_be_analyzed_and_approval_needs_confirmation(self):
        record = self.store.create("Refund USD 5 from pi_1")
        self.assertEqual(self.post(f"/ui/requests/{record['id']}/analyze").status_code, 303)
        run = self.store.history(record["id"])["analyses"][-1]
        self.assertEqual(self.act(run, "approve", payment_id="pi_1", amount="500", currency="usd").status_code, 400)
        self.assertEqual(self.act(run, "unrecognized").status_code, 404)


if __name__ == "__main__":
    unittest.main()
