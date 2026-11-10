import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import stripe

from payresolve import create_app
from payresolve.config import Settings
from payresolve.stripe_gateway import SandboxError, StripeSandbox

TEST_KEY = "sk_test_fixture"


class StripeTests(unittest.TestCase):
    def test_live_and_publishable_keys_rejected_before_client_creation(self):
        with patch("payresolve.stripe_gateway.stripe.StripeClient") as client:
            for key in ["sk_live_fake", "rk_live_fake", "pk_test_fake", "invalid", "sk_test_"]:
                with self.subTest(key=key), self.assertRaises(SandboxError):
                    StripeSandbox(key)
            client.assert_not_called()

    def test_missing_key_does_not_call_stripe(self):
        with patch("payresolve.stripe_gateway.stripe.StripeClient") as client:
            with self.assertRaises(SandboxError) as error:
                StripeSandbox("").check_connection()
            self.assertEqual(error.exception.code, "stripe_not_configured")
            client.assert_not_called()

    def test_real_sdk_builds_read_only_balance_request(self):
        # Exercise the installed SDK and its deserializer; replace only HTTP I/O.
        payload = json.dumps({"object": "balance", "livemode": False, "available": [], "pending": []})
        with patch.object(stripe.RequestsClient, "request", return_value=(payload, 200, {})) as transport:
            result = StripeSandbox(TEST_KEY).check_connection()
        self.assertEqual(result, {"status": "connected", "livemode": False})
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[:2], ("get", "https://api.stripe.com/v1/balance"))

    def test_restricted_test_key_is_supported(self):
        with patch("payresolve.stripe_gateway.stripe.StripeClient") as client:
            client.return_value.v1.balance.retrieve.return_value = SimpleNamespace(livemode=False)
            self.assertEqual(StripeSandbox("rk_test_fake").check_connection()["status"], "connected")
            self.assertEqual(client.call_args.kwargs["max_network_retries"], 1)

    def test_live_or_missing_mode_fails_closed(self):
        with patch("payresolve.stripe_gateway.stripe.StripeClient") as client:
            for balance in [SimpleNamespace(livemode=True), SimpleNamespace(),
                            SimpleNamespace(livemode=0), SimpleNamespace(livemode="false")]:
                client.return_value.v1.balance.retrieve.return_value = balance
                with self.assertRaises(SandboxError) as error:
                    StripeSandbox(TEST_KEY).check_connection()
                self.assertEqual(error.exception.code, "stripe_mode_rejected")

    def test_api_errors_are_sanitized(self):
        cases = [(stripe.AuthenticationError, "stripe_authentication_failed"),
                 (stripe.PermissionError, "stripe_permission_denied"),
                 (stripe.APIConnectionError, "stripe_connection_failed"),
                 (stripe.RateLimitError, "stripe_rate_limited"),
                 (stripe.StripeError, "stripe_unavailable")]
        with patch("payresolve.stripe_gateway.stripe.StripeClient") as client:
            for error_type, code in cases:
                with self.subTest(error_type=error_type):
                    client.return_value.v1.balance.retrieve.side_effect = error_type(TEST_KEY)
                    with self.assertRaises(SandboxError) as error:
                        StripeSandbox(TEST_KEY).check_connection()
                    self.assertEqual(error.exception.code, code)
                    self.assertNotIn(TEST_KEY, str(error.exception))

    def test_status_endpoint_and_intake_do_not_disclose_key(self):
        with TemporaryDirectory() as directory:
            app = create_app(Settings(Path(directory) / "test.db", TEST_KEY))
            with patch("payresolve.stripe_gateway.stripe.StripeClient") as stripe_client:
                client = app.test_client()
                client.post("/requests", json={"message": "Refund my payment"})
                client.get("/health")
                stripe_client.assert_not_called()
                stripe_client.return_value.v1.balance.retrieve.side_effect = stripe.AuthenticationError(TEST_KEY)
                response = client.get("/stripe/status")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["error"]["code"], "stripe_authentication_failed")
                self.assertNotIn(TEST_KEY, response.get_data(as_text=True))
