from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from payresolve.operations import OperationStore
from payresolve.m1 import run_fixtures
from payresolve.stripe_gateway import SandboxError
from payresolve.workflow import REPLAY_WINDOW_SECONDS, SandboxWorkflow


class FakeStripe:
    """Stateful Stripe stand-in: idempotent writes, independent read state."""

    def __init__(self):
        self.payments = {}
        self.refunds = {}
        self.keys = {}
        self.refund_posts = []
        self.lose_response = False
        self.fail_refund_read = False
        self.refund_status = "succeeded"
        self.before_post = lambda key: None

    def check_connection(self):
        return {"status": "connected", "livemode": False}

    def create_payment(self, params, key):
        if "payment_method_types" in params or params.get("allowed_payment_method_types") != ["card"]:
            raise SandboxError("stripe_request_rejected", "Invalid payment method selection")
        self.before_post(key)
        if key in self.keys:
            return self.keys[key]
        payment_id = f"pi_{len(self.payments) + 1}"
        paid = params.get("confirm", False)
        self.payments[payment_id] = {
            "id": payment_id, "amount": params["amount"], "currency": params["currency"],
            "status": "succeeded" if paid else "requires_payment_method", "livemode": False,
            "operation_key": params["metadata"]["payresolve_operation"],
            "charge_id": f"ch_{len(self.payments) + 1}" if paid else None,
            "paid": paid, "captured": paid, "disputed": False,
            "amount_captured": params["amount"] if paid else 0,
            "amount_refunded": 0, "refundable_amount": params["amount"] if paid else 0,
        }
        self.keys[key] = payment_id
        return payment_id

    def get_payment(self, payment_id):
        if payment_id not in self.payments:
            raise SandboxError("stripe_request_rejected", "Missing payment", 400)
        return deepcopy(self.payments[payment_id])

    def create_refund(self, params, key):
        self.before_post(key)
        self.refund_posts.append((deepcopy(params), key))
        if key not in self.keys:
            payment = self.payments[params["payment_intent"]]
            refund_id = f"re_{len(self.refunds) + 1}"
            self.keys[key] = refund_id
            self.refunds[refund_id] = {
                "id": refund_id, "payment_intent": payment["id"], "charge": payment["charge_id"],
                "currency": payment["currency"], "amount": params["amount"], "status": self.refund_status,
                "operation_key": params["metadata"]["payresolve_operation"],
            }
            if self.refund_status == "succeeded":
                payment["amount_refunded"] += params["amount"]
                payment["refundable_amount"] -= params["amount"]
        if self.lose_response:
            self.lose_response = False
            raise SandboxError("stripe_connection_failed", "Response lost")
        return self.keys[key]

    def get_refund(self, refund_id):
        if self.fail_refund_read:
            self.fail_refund_read = False
            raise SandboxError("stripe_connection_failed", "Read failed")
        return deepcopy(self.refunds[refund_id])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.db"
        self.gateway = FakeStripe()
        self.workflow = SandboxWorkflow(OperationStore(self.path), self.gateway)

    def payment(self, amount=3999, **kwargs):
        return self.workflow.create_payment("payment", amount, **kwargs)["payment"]["id"]

    def test_create_and_get_payment_persist_across_restart(self):
        payment_id = self.payment()
        restarted = SandboxWorkflow(OperationStore(self.path), self.gateway)
        repeated = restarted.create_payment("payment", 3999)
        self.assertEqual(repeated["payment"]["id"], payment_id)
        self.assertEqual(len(self.gateway.payments), 1)
        self.assertEqual(restarted.store.list_payments()[0]["id"], payment_id)

    def test_uncaptured_payment_creation_is_not_verified(self):
        self.payment()
        self.gateway.payments["pi_1"]["captured"] = False
        with self.assertRaises(SandboxError) as error:
            self.workflow.verify("payment")
        self.assertEqual(error.exception.code, "verification_mismatch")

    def test_journal_is_committed_before_any_post(self):
        def check(key):
            with self.workflow.store.connect() as connection:
                self.assertIsNotNone(connection.execute("SELECT 1 FROM stripe_operations WHERE stripe_key = ?", (key,)).fetchone())
        self.gateway.before_post = check
        self.workflow.refund("refund", self.payment(), 1000)

    def test_partial_then_full_remaining_refund(self):
        payment_id = self.payment()
        partial = self.workflow.refund("partial", payment_id, 1000)
        self.assertEqual(partial["status"], "verified")
        self.assertEqual(partial["payment"]["refundable_amount"], 2999)
        full = self.workflow.refund("full", payment_id)
        self.assertEqual(full["refund"]["amount"], 2999)
        self.assertEqual(full["payment"]["refundable_amount"], 0)
        self.assertEqual(self.workflow.verify("partial")["status"], "verified")

    def test_same_operation_after_restart_never_posts_again(self):
        payment_id = self.payment()
        original = self.workflow.refund("refund", payment_id, 1000)
        restarted = SandboxWorkflow(OperationStore(self.path), self.gateway)
        self.assertEqual(restarted.refund("refund", payment_id, 1000), original)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        with self.assertRaises(SandboxError) as error:
            restarted.refund("refund", payment_id, 1001)
        self.assertEqual(error.exception.code, "operation_conflict")

    def test_ambiguous_response_replays_identical_request_and_key(self):
        payment_id = self.payment()
        self.gateway.lose_response = True
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id)
        self.assertEqual(self.gateway.payments[payment_id]["refundable_amount"], 0)
        restarted = SandboxWorkflow(OperationStore(self.path), self.gateway)
        result = restarted.refund("refund", payment_id)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.gateway.refund_posts[0], self.gateway.refund_posts[1])
        self.assertEqual(len(self.gateway.refunds), 1)

    def test_verification_failure_keeps_id_and_recovers_without_post(self):
        payment_id = self.payment()
        self.gateway.fail_refund_read = True
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id, 1000)
        self.assertIsNotNone(self.workflow.operation("refund")["stripe_id"])
        self.assertEqual(self.workflow.verify("refund")["status"], "verified")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_expired_unknown_operation_fails_closed(self):
        payment_id = self.payment()
        self.gateway.lose_response = True
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id, 1000)
        with self.workflow.store.connect() as connection:
            connection.execute("UPDATE stripe_operations SET created_at = ? WHERE operation_id = 'refund'", (time.time() - REPLAY_WINDOW_SECONDS - 1,))
        with self.assertRaises(SandboxError) as error:
            self.workflow.refund("refund", payment_id, 1000)
        self.assertEqual(error.exception.code, "reconciliation_required")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_pending_is_not_success_and_blocks_second_operation(self):
        payment_id = self.payment()
        self.gateway.refund_status = "pending"
        result = self.workflow.refund("refund", payment_id, 1000)
        self.assertEqual(result["status"], "pending")
        with self.assertRaises(SandboxError) as error:
            self.workflow.refund("second", payment_id, 1000)
        self.assertEqual(error.exception.code, "unresolved_operation")
        refund = next(iter(self.gateway.refunds.values()))
        refund["status"] = "succeeded"
        self.gateway.payments[payment_id].update(amount_refunded=1000, refundable_amount=2999)
        self.assertEqual(self.workflow.verify("refund")["status"], "verified")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_failed_refund_is_not_success(self):
        self.gateway.refund_status = "failed"
        result = self.workflow.refund("refund", self.payment(), 1000)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["payment"]["amount_refunded"], 0)

    def test_invalid_amounts_and_currency_never_post(self):
        payment_id = self.payment()
        for amount in [0, -1, True, 1.5, "1000", 4000]:
            with self.subTest(amount=amount), self.assertRaises(SandboxError):
                self.workflow.refund("invalid", payment_id, amount)
        with self.assertRaises(SandboxError):
            self.workflow.refund("currency", payment_id, 1000, currency="eur")
        self.assertEqual(self.gateway.refund_posts, [])

    def test_unpaid_failed_disputed_uncaptured_and_refunded_payments_rejected(self):
        payment_id = self.payment()
        initial = deepcopy(self.gateway.payments[payment_id])
        for changes in [{"status": "requires_payment_method"}, {"status": "canceled"},
                        {"captured": False}, {"disputed": True}, {"paid": False},
                        {"refundable_amount": 0, "amount_refunded": 3999}]:
            self.gateway.payments[payment_id] = {**initial, **changes}
            with self.subTest(changes=changes), self.assertRaises(SandboxError):
                self.workflow.refund("invalid", payment_id)
        self.assertEqual(self.gateway.refund_posts, [])

    def test_unmanaged_and_nonexistent_payment_rejected(self):
        with self.assertRaises(SandboxError) as error:
            self.workflow.refund("refund", "pi_missing", 100)
        self.assertEqual(error.exception.code, "unmanaged_payment")
        with self.assertRaises(SandboxError):
            self.workflow.get_payment("pi_missing")

    def test_verification_mismatch_is_not_marked_successful(self):
        payment_id = self.payment()
        self.gateway.fail_refund_read = True
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id, 1000)
        next(iter(self.gateway.refunds.values()))["amount"] = 999
        with self.assertRaises(SandboxError) as error:
            self.workflow.verify("refund")
        self.assertEqual(error.exception.code, "verification_mismatch")
        self.assertEqual(self.workflow.operation("refund")["status"], "unresolved")

    def test_later_verification_mismatch_invalidates_cached_success(self):
        payment_id = self.payment()
        self.workflow.refund("refund", payment_id, 1000)
        next(iter(self.gateway.refunds.values()))["amount"] = 999
        with self.assertRaises(SandboxError):
            self.workflow.verify("refund")
        saved = self.workflow.operation("refund")
        self.assertEqual(saved["status"], "unresolved")
        self.assertIsNone(saved["result"])
        self.assertIsNotNone(saved["stripe_id"])
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id, 1000)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        next(iter(self.gateway.refunds.values()))["amount"] = 1000
        self.assertEqual(self.workflow.verify("refund")["status"], "verified")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_concurrent_same_operation_creates_one_refund(self):
        payment_id = self.payment()
        def execute(_):
            return SandboxWorkflow(OperationStore(self.path), self.gateway).refund("refund", payment_id, 1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_unpaid_fixture_is_verified_without_a_charge(self):
        payment_id = self.payment(unpaid=True)
        self.assertEqual(self.workflow.get_payment(payment_id)["status"], "requires_payment_method")
        with self.assertRaises(SandboxError):
            self.workflow.refund("refund", payment_id)

    def test_fixture_run_can_be_repeated_without_new_writes(self):
        first = run_fixtures(self.workflow, "test-run")
        second = run_fixtures(self.workflow, "test-run")
        self.assertEqual(first, second)
        self.assertEqual(len(self.gateway.payments), 4)
        self.assertEqual(len(self.gateway.refunds), 2)
        self.assertEqual(len(self.gateway.refund_posts), 2)

    def test_distinct_refund_keys_cannot_bypass_unresolved_operation(self):
        payment_id = self.payment()
        self.gateway.lose_response = True
        with self.assertRaises(SandboxError):
            self.workflow.refund("first", payment_id, 1000)
        with self.assertRaises(SandboxError) as error:
            self.workflow.refund("second", payment_id, 1000)
        self.assertEqual(error.exception.code, "unresolved_operation")
        self.assertEqual(len(self.gateway.refund_posts), 1)
