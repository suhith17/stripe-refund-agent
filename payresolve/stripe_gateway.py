"""Stripe sandbox transport. Return allowlisted fields, never client secrets."""

import re

import stripe


class SandboxError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class StripeSandbox:
    def __init__(self, secret_key: str):
        if secret_key and not re.fullmatch(r"(?:sk|rk)_test_[A-Za-z0-9]+", secret_key):
            raise SandboxError("invalid_stripe_key", "Use a Stripe sandbox secret or restricted test key.")
        self._secret_key = secret_key

    def _call(self, action):
        if not self._secret_key:
            raise SandboxError("stripe_not_configured", "Set STRIPE_SECRET_KEY in your local .env file.")
        client = stripe.StripeClient(
            self._secret_key,
            http_client=stripe.RequestsClient(timeout=10),
            max_network_retries=1,
        )
        try:
            return action(client)
        except stripe.AuthenticationError:
            raise SandboxError("stripe_authentication_failed", "Stripe rejected the sandbox key.") from None
        except stripe.PermissionError:
            raise SandboxError("stripe_permission_denied", "The sandbox key lacks permission for this operation.") from None
        except stripe.APIConnectionError:
            raise SandboxError("stripe_connection_failed", "Could not connect to Stripe. Check network access.") from None
        except stripe.RateLimitError:
            raise SandboxError("stripe_rate_limited", "Stripe rate limit reached. Try again later.") from None
        except stripe.InvalidRequestError:
            raise SandboxError("stripe_request_rejected", "Stripe rejected the operation or identifier.", 400) from None
        except stripe.StripeError:
            raise SandboxError("stripe_unavailable", "Stripe could not complete the connection check.", 502) from None

    @staticmethod
    def _require_test(obj):
        if getattr(obj, "livemode", None) is not False:
            raise SandboxError("stripe_mode_rejected", "Stripe did not confirm a sandbox response.", 502)

    def check_connection(self) -> dict:
        balance = self._call(lambda client: client.v1.balance.retrieve())
        self._require_test(balance)
        return {"status": "connected", "livemode": False}

    @staticmethod
    def _operation_key(obj):
        metadata = getattr(obj, "metadata", {})
        return metadata["payresolve_operation"] if "payresolve_operation" in metadata else None

    def create_payment(self, params: dict, idempotency_key: str) -> str:
        payment = self._call(lambda client: client.v1.payment_intents.create(
            params, options={"idempotency_key": idempotency_key}))
        self._require_test(payment)
        return payment.id

    def get_payment(self, payment_id: str) -> dict:
        payment = self._call(lambda client: client.v1.payment_intents.retrieve(
            payment_id, params={"expand": ["latest_charge"]}))
        self._require_test(payment)
        charge = payment.latest_charge
        if charge is not None:
            self._require_test(charge)
            if charge.payment_intent != payment.id or charge.currency != payment.currency:
                raise SandboxError("stripe_response_invalid", "Payment and charge do not match.", 502)
        captured = charge.amount_captured if charge else 0
        refunded = charge.amount_refunded if charge else 0
        if type(captured) is not int or type(refunded) is not int or not 0 <= refunded <= captured:
            raise SandboxError("stripe_response_invalid", "Stripe returned invalid payment amounts.", 502)
        return {
            "id": payment.id, "status": payment.status, "amount": payment.amount,
            "currency": payment.currency, "livemode": False,
            "operation_key": self._operation_key(payment),
            "charge_id": charge.id if charge else None,
            "paid": charge.paid if charge else False,
            "captured": charge.captured if charge else False,
            "disputed": charge.disputed if charge else False,
            "amount_captured": captured, "amount_refunded": refunded,
            "refundable_amount": captured - refunded,
        }

    def create_refund(self, params: dict, idempotency_key: str) -> str:
        refund = self._call(lambda client: client.v1.refunds.create(
            params, options={"idempotency_key": idempotency_key}))
        return refund.id

    def get_refund(self, refund_id: str) -> dict:
        refund = self._call(lambda client: client.v1.refunds.retrieve(refund_id))
        # Refund has no livemode field. The workflow verifies its linked payment.
        return {
            "id": refund.id, "status": refund.status, "amount": refund.amount,
            "currency": refund.currency, "payment_intent": refund.payment_intent,
            "charge": refund.charge, "operation_key": self._operation_key(refund),
        }
