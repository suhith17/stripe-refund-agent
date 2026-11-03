import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import stripe

from payresolve.operations import OperationStore
from payresolve.stripe_gateway import StripeSandbox


class PaymentTransportTests(unittest.TestCase):
    def test_payment_post_passes_idempotency_and_test_method(self):
        response = json.dumps({"id": "pi_test", "object": "payment_intent", "livemode": False})
        with patch.object(stripe.RequestsClient, "request", return_value=(response, 200, {})) as transport:
            result = StripeSandbox("sk_test_fake").create_payment(
                {"amount": 3999, "currency": "usd", "payment_method": "pm_card_visa",
                 "allowed_payment_method_types": ["card"], "confirm": True}, "persisted-key")
        self.assertEqual(result, "pi_test")
        self.assertEqual(transport.call_args.args[:2], ("post", "https://api.stripe.com/v1/payment_intents"))
        self.assertEqual(transport.call_args.args[2]["Idempotency-Key"], "persisted-key")

    def test_snapshot_omits_client_secret_and_checks_charge(self):
        response = json.dumps({
            "id": "pi_test", "object": "payment_intent", "livemode": False,
            "status": "succeeded", "amount": 3999, "currency": "usd",
            "client_secret": "must-not-be-returned", "metadata": {},
            "latest_charge": {"id": "ch_test", "object": "charge", "livemode": False,
                              "payment_intent": "pi_test", "currency": "usd", "paid": True,
                              "captured": True, "disputed": False,
                              "amount_captured": 3999, "amount_refunded": 1000},
        })
        with patch.object(stripe.RequestsClient, "request", return_value=(response, 200, {})):
            result = StripeSandbox("sk_test_fake").get_payment("pi_test")
        self.assertEqual(result["refundable_amount"], 2999)
        self.assertNotIn("must-not-be-returned", json.dumps(result))

    def test_additive_schema_preserves_intake(self):
        with TemporaryDirectory() as directory:
            store = OperationStore(Path(directory) / "test.db")
            request = store.create("Original intake")
            reopened = OperationStore(store.path)
            self.assertEqual(reopened.get(request["id"]), request)
            self.assertEqual(reopened.list_payments(), [])

    def test_refund_sdk_round_trip_without_livemode_field(self):
        payload = json.dumps({"id": "re_test", "object": "refund", "amount": 1000,
                              "currency": "usd", "payment_intent": "pi_test", "charge": "ch_test",
                              "status": "succeeded", "metadata": {"payresolve_operation": "saved-key"}})
        gateway = StripeSandbox("sk_test_fake")
        with patch.object(stripe.RequestsClient, "request", return_value=(payload, 200, {})) as transport:
            refund_id = gateway.create_refund({"payment_intent": "pi_test", "amount": 1000}, "saved-key")
            self.assertEqual(transport.call_args.args[2]["Idempotency-Key"], "saved-key")
            result = gateway.get_refund(refund_id)
            self.assertEqual(transport.call_args.args[:2], ("get", "https://api.stripe.com/v1/refunds/re_test"))
        self.assertEqual(result["operation_key"], "saved-key")
        self.assertEqual(result["amount"], 1000)
