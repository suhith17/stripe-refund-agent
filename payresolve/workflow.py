"""Deterministic sandbox operations, independent of natural-language intake."""

import json
import re
import time
from uuid import uuid4

from .operations import OperationStore
from .stripe_gateway import SandboxError, StripeSandbox

REPLAY_WINDOW_SECONDS = 23 * 60 * 60


def reject(code, message):
    raise SandboxError(code, message, 409)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SandboxWorkflow:
    def __init__(self, store: OperationStore, gateway: StripeSandbox):
        self.store = store
        self.gateway = gateway

    def create_payment(self, operation_id: str, amount: int, *, unpaid: bool = False):
        if type(amount) is not int or not 50 <= amount <= 1_000_000 or type(unpaid) is not bool:
            reject("invalid_payment", "Use an integer USD amount from 50 to 1000000 cents.")
        return self._run(operation_id, "payment", {"amount": amount, "currency": "usd", "unpaid": unpaid})

    def refund(self, operation_id: str, payment_id: str, amount: int | None = None, *, currency="usd"):
        if not isinstance(payment_id, str) or not re.fullmatch(r"pi_[A-Za-z0-9]+", payment_id):
            reject("invalid_payment_id", "Provide a PaymentIntent identifier.")
        if amount is not None and (type(amount) is not int or amount <= 0):
            reject("invalid_refund_amount", "Refund amount must be a positive integer in cents.")
        if currency != "usd":
            reject("currency_mismatch", "M1 fixtures support only USD.")
        return self._run(operation_id, "refund", {"payment_id": payment_id, "amount": amount, "currency": currency})

    def get_payment(self, payment_id):
        if not isinstance(payment_id, str) or not re.fullmatch(r"pi_[A-Za-z0-9]+", payment_id):
            reject("invalid_payment_id", "Provide a PaymentIntent identifier.")
        payment = self.gateway.get_payment(payment_id)
        with self.store.locked() as connection:
            row = connection.execute("SELECT operation_id FROM sandbox_payments WHERE payment_id = ?", (payment_id,)).fetchone()
            if row:
                self.store.save_payment(connection, row[0], payment)
        return payment

    def operation(self, operation_id):
        with self.store.connect() as connection:
            op = self.store.operation(connection, operation_id)
        if not op:
            reject("operation_not_found", "No operation exists with that ID.")
        return {"operation_id": operation_id, "kind": op["kind"], "status": op["status"],
                "stripe_id": op["stripe_id"], "last_error": op["last_error"],
                "result": json.loads(op["result_json"]) if op["result_json"] else None}

    def _prepare(self, operation_id, kind, inputs):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
            reject("invalid_operation_id", "Use 1 to 100 letters, digits, underscores or hyphens for the operation ID.")
        with self.store.locked() as connection:
            existing = self.store.operation(connection, operation_id)
            if existing:
                if existing["kind"] != kind or existing["input_json"] != canonical(inputs):
                    reject("operation_conflict", "This operation ID already belongs to different parameters.")
                return
            key = str(uuid4())
            metadata = {"project": "payresolve_m1", "payresolve_operation": key}
            before = {}
            target = inputs.get("payment_id")
            if kind == "payment":
                self.gateway.check_connection()
                params = {"amount": inputs["amount"], "currency": "usd", "allowed_payment_method_types": ["card"],
                          "metadata": metadata}
                if not inputs["unpaid"]:
                    params.update(payment_method="pm_card_visa", confirm=True)
            else:
                local = connection.execute("SELECT operation_id FROM sandbox_payments WHERE payment_id = ?", (target,)).fetchone()
                if not local:
                    reject("unmanaged_payment", "Refunds are limited to payments created in this local M1 database.")
                outstanding = connection.execute(
                    "SELECT 1 FROM stripe_operations WHERE target = ? AND status IN ('unresolved', 'pending')", (target,),
                ).fetchone()
                if outstanding:
                    reject("unresolved_operation", "Resolve the earlier refund using its original operation ID first.")
                before = self.gateway.get_payment(target)
                owner = self.store.operation(connection, local[0])
                if before["livemode"] is not False or before["operation_key"] != owner["stripe_key"]:
                    reject("payment_identity_mismatch", "The sandbox payment does not match the local record.")
                if before["status"] != "succeeded" or before["paid"] is not True or before["captured"] is not True:
                    reject("payment_not_refundable", "Only successful, captured payments can be refunded.")
                if before["disputed"] is not False:
                    reject("payment_disputed", "Disputed payments require manual review.")
                if before["currency"] != inputs["currency"]:
                    reject("currency_mismatch", "Refund currency must match the payment.")
                amount = inputs["amount"] if inputs["amount"] is not None else before["refundable_amount"]
                if not 0 < amount <= before["refundable_amount"]:
                    reject("invalid_refund_amount", "Refund exceeds the remaining amount, or the payment is fully refunded.")
                params = {"payment_intent": target, "amount": amount, "reason": "requested_by_customer", "metadata": metadata}
            connection.execute(
                "INSERT INTO stripe_operations (operation_id, kind, target, input_json, params_json, before_json, stripe_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (operation_id, kind, target, canonical(inputs), canonical(params), canonical(before), key, time.time()),
            )
        # The immutable parameters and Stripe key are committed BEFORE any POST.

    def _run(self, operation_id, kind, inputs):
        self._prepare(operation_id, kind, inputs)
        return self._execute(operation_id)

    def _execute(self, operation_id, *, read_only=False):
        error = None
        result = None
        with self.store.locked() as connection:
            op = self.store.operation(connection, operation_id)
            if not op:
                reject("operation_not_found", "No operation exists with that ID.")
            if op["status"] in ("verified", "failed") and not read_only:
                return json.loads(op["result_json"])
            params = json.loads(op["params_json"])
            try:
                if not op["stripe_id"]:
                    if read_only:
                        reject("unknown_stripe_id", "Retry the original command to recover the Stripe ID with its saved key.")
                    if time.time() - op["created_at"] >= REPLAY_WINDOW_SECONDS:
                        reject("reconciliation_required", "The 23-hour replay window expired. Reconcile in Stripe; do not use a new operation ID.")
                    method = self.gateway.create_payment if op["kind"] == "payment" else self.gateway.create_refund
                    op["stripe_id"] = method(params, op["stripe_key"])
                    connection.execute("UPDATE stripe_operations SET stripe_id = ? WHERE operation_id = ?", (op["stripe_id"], operation_id))
                result = self._verify(connection, op, params)
                connection.execute(
                    "UPDATE stripe_operations SET status = ?, result_json = ?, last_error = NULL WHERE operation_id = ?",
                    (result["status"], canonical(result), operation_id),
                )
            except SandboxError as caught:
                # Commit any known Stripe ID even if subsequent GET/verification fails.
                # Only a fixed error code is stored, never upstream text or credentials.
                connection.execute("UPDATE stripe_operations SET last_error = ? WHERE operation_id = ?", (caught.code, operation_id))
                if caught.code in ("verification_mismatch", "stripe_mode_rejected", "stripe_response_invalid"):
                    # Contradictory evidence invalidates a cached success. Keep the
                    # Stripe ID so recovery remains retrieval-only, never a new POST.
                    connection.execute(
                        "UPDATE stripe_operations SET status = 'unresolved', result_json = NULL WHERE operation_id = ?",
                        (operation_id,),
                    )
                error = caught
        if error:
            raise error
        return result

    def verify(self, operation_id):
        """Retrieve final state without ever submitting a new Stripe write."""
        return self._execute(operation_id, read_only=True)

    def _verify(self, connection, op, params):
        if op["kind"] == "payment":
            payment = self.gateway.get_payment(op["stripe_id"])
            inputs = json.loads(op["input_json"])
            expected_status = "requires_payment_method" if inputs["unpaid"] else "succeeded"
            if (payment["id"] != op["stripe_id"] or payment["operation_key"] != op["stripe_key"]
                    or payment["amount"] != params["amount"] or payment["currency"] != params["currency"]
                    or payment["livemode"] is not False or payment["status"] != expected_status):
                reject("verification_mismatch", "Retrieved payment does not match the expected fixture.")
            if not inputs["unpaid"] and (payment["paid"] is not True or payment["captured"] is not True
                                          or payment["amount_captured"] != params["amount"]):
                reject("verification_mismatch", "The test payment has not been fully captured.")
            self.store.save_payment(connection, op["operation_id"], payment)
            return {"operation_id": op["operation_id"], "status": "verified", "payment": payment}
        refund = self.gateway.get_refund(op["stripe_id"])
        payment = self.gateway.get_payment(params["payment_intent"])
        before = json.loads(op["before_json"])
        if (refund["id"] != op["stripe_id"] or refund["payment_intent"] != params["payment_intent"]
                or refund["charge"] != before["charge_id"] or refund["amount"] != params["amount"]
                or refund["currency"] != before["currency"] or refund["operation_key"] != op["stripe_key"]
                or payment["id"] != before["id"] or payment["charge_id"] != before["charge_id"]
                or payment["currency"] != before["currency"] or payment["livemode"] is not False
                or payment["amount_captured"] != before["amount_captured"]):
            reject("verification_mismatch", "Retrieved refund and payment do not match the saved operation.")
        if refund["status"] == "succeeded":
            if payment["amount_refunded"] < before["amount_refunded"] + params["amount"]:
                reject("verification_mismatch", "Stripe's payment state does not yet reflect the refund.")
            status = "verified"
        elif refund["status"] in ("failed", "canceled"):
            status = "failed"
        elif refund["status"] in ("pending", "requires_action"):
            status = "pending"
        else:
            reject("verification_mismatch", "Stripe returned an unexpected refund status.")
        local = connection.execute("SELECT operation_id FROM sandbox_payments WHERE payment_id = ?", (payment["id"],)).fetchone()
        self.store.save_payment(connection, local[0], payment)
        return {"operation_id": op["operation_id"], "status": status, "refund": refund, "payment": payment}
