import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from payresolve import create_app
from payresolve.config import Settings


class BackendTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(Path(directory.name) / "requests.db")
        self.client = create_app(self.settings).test_client()

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok", "database": "ok"})

    def test_unknown_route_returns_json(self):
        response = self.client.get("/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"]["code"], "not_found")

    def test_request_survives_application_restart(self):
        message = "Refund the duplicate $39.99 payment for order 4812."
        response = self.client.post("/requests", json={"message": message})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["status"], "received")
        restarted = create_app(self.settings).test_client()
        retrieved = restarted.get(response.headers["Location"])
        self.assertEqual(retrieved.status_code, 200)
        self.assertEqual(retrieved.json, response.json)
        self.assertEqual(retrieved.json["message"], message)

    def test_sql_and_unicode_are_stored_as_text(self):
        message = "Refund please — '); DROP TABLE refund_requests; --"
        created = self.client.post("/requests", json={"message": message})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.get(created.headers["Location"]).json["message"], message)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_invalid_input_is_rejected(self):
        for body in [None, [], {}, {"message": 12}, {"message": True},
                     {"message": " "}, {"message": "x" * 4001},
                     {"message": "a\x00b"}, {"message": "refund", "status": "refunded"}]:
            with self.subTest(body_type=type(body).__name__):
                response = self.client.post("/requests", json=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)

    def test_malformed_json_and_wrong_content_type(self):
        self.assertEqual(self.client.post("/requests", data="{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/requests", data="message=refund").status_code, 415)

    def test_body_limit(self):
        self.assertEqual(self.client.post("/requests", json={"message": "x" * 17000}).status_code, 413)

    def test_missing_request(self):
        self.assertEqual(self.client.get(f"/requests/{uuid4()}").status_code, 404)

    def test_key_not_in_settings_repr(self):
        self.assertNotIn("sk_test_example", repr(Settings(self.settings.database_path, "sk_test_example")))
