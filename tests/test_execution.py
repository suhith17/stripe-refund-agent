from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.__main__ import main
from payresolve.analysis import DryRunPolicy
from payresolve.config import Settings
from payresolve.execution import ExecutionStore, ReviewedExecution
from payresolve.stripe_gateway import SandboxError
from payresolve.workflow import SandboxWorkflow
from tests.test_analysis import intent
from tests.test_workflow import FakeStripe


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.db"
        self.store = ExecutionStore(self.path)
        self.gateway = FakeStripe()
        SandboxWorkflow(self.store, self.gateway).create_payment("fixture", 1000)
        self.message = "Refund USD 5 from pi_1"
        self.request = self.store.create(self.message)
        self.run = self.proposal()
        self.service = ReviewedExecution(self.store, self.gateway)

    def proposal(self):
        run = self.store.start_analysis(self.request["id"], "gemini-3.5-flash-lite")
        run.update(status="completed", intent=intent().model_dump(),
                   decision=DryRunPolicy(self.store, self.gateway.get_payment).assess(self.message, intent()))
        return self.store.finish_analysis(run)

    def approve(self):
        return self.store.approve(self.run["id"], "pi_1", 500, "usd")

    def test_requires_approval_and_approval_itself_never_posts(self):
        with self.assertRaises(SandboxError) as caught:
            self.service.execute(self.run["id"])
        self.assertEqual(caught.exception.code, "approval_required")
        approved = self.approve()
        self.assertEqual(self.approve(), approved)
        self.assertEqual(self.store.execution(self.run["id"])["status"], "approved")
        self.assertEqual(self.gateway.refund_posts, [])

    def test_only_exact_completed_proposals_can_be_approved(self):
        for payment, amount, currency in [("pi_2", 500, "usd"), ("pi_1", 499, "usd"),
                                          ("pi_1", True, "usd"), ("pi_1", 0, "usd"),
                                          ("pi_1", 500, "eur")]:
            with self.subTest(payment=payment, amount=amount, currency=currency), self.assertRaises(SandboxError):
                self.store.approve(self.run["id"], payment, amount, currency)
        for action in ("clarify", "reject", "no_action"):
            self.run["decision"]["action"] = action
            self.store.finish_analysis(self.run)
            with self.assertRaises(SandboxError):
                self.approve()
        self.run["decision"]["action"] = "propose_refund"
        self.run["status"] = "failed"
        self.store.finish_analysis(self.run)
        with self.assertRaises(SandboxError):
            self.approve()
        self.assertEqual(self.gateway.refund_posts, [])

    def test_execute_and_replay_after_restart_create_one_refund(self):
        approval = self.approve()
        result = self.service.execute(self.run["id"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["result"]["refund"]["amount"], 500)
        restarted = ReviewedExecution(ExecutionStore(self.path), self.gateway)
        self.assertEqual(restarted.execute(self.run["id"]), result)
        self.assertEqual(restarted.execute(self.run["id"], verify_only=True), result)
        self.assertEqual(len(self.gateway.refund_posts), 1)
        self.assertEqual(result["approval"]["operation_id"], approval["operation_id"])
        self.assertIs(self.store.analysis(self.run["id"])["executed"], False)

    def test_reanalysis_cannot_create_second_approval_for_same_request(self):
        second = self.proposal()
        self.approve()
        self.service.execute(self.run["id"])
        with self.assertRaises(SandboxError) as caught:
            self.store.approve(second["id"], "pi_1", 500, "usd")
        self.assertEqual(caught.exception.code, "request_already_approved")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_changed_balance_and_payment_state_are_rechecked_before_post(self):
        self.approve()
        for changes in ({"amount_refunded": 800, "refundable_amount": 200},
                        {"disputed": True}, {"captured": False}):
            original = self.gateway.payments["pi_1"].copy()
            self.gateway.payments["pi_1"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(SandboxError):
                self.service.execute(self.run["id"])
            self.assertIsNotNone(self.store.approval(self.run["id"])["last_error"])
            self.gateway.payments["pi_1"] = original
        self.assertEqual(self.gateway.refund_posts, [])

    def test_approval_is_committed_before_post_and_immutable(self):
        approval = self.approve()
        self.run["decision"]["amount_minor"] = 900
        self.store.finish_analysis(self.run)

        # Read using existing schema to avoid attempting schema DDL under the writer lock.
        def check_committed(key):
            with self.store.connect() as connection:
                row = connection.execute("SELECT amount_minor FROM refund_approvals WHERE analysis_id = ?", (self.run["id"],)).fetchone()
                self.assertEqual(row[0], 500)
        self.gateway.before_post = check_committed
        self.assertEqual(self.service.execute(self.run["id"])["result"]["refund"]["amount"], 500)
        self.assertEqual(self.store.approval(self.run["id"])["operation_id"], approval["operation_id"])
        with self.assertRaises(SandboxError):
            self.store.approve(self.run["id"], "pi_1", 900, "usd")

    def test_lost_response_recovers_original_refund_with_same_key(self):
        self.approve()
        self.gateway.lose_response = True
        with self.assertRaises(SandboxError):
            self.service.execute(self.run["id"])
        self.assertEqual(self.store.execution(self.run["id"])["status"], "unresolved")
        with self.assertRaises(SandboxError) as caught:
            self.service.execute(self.run["id"], verify_only=True)
        self.assertEqual(caught.exception.code, "unknown_stripe_id")
        self.assertEqual(len(self.gateway.refund_posts), 1)
        result = self.service.execute(self.run["id"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(self.gateway.refunds), 1)
        self.assertEqual(self.gateway.refund_posts[0], self.gateway.refund_posts[1])

    def test_pending_refund_is_not_success_and_verify_never_reposts(self):
        self.approve()
        self.gateway.refund_status = "pending"
        first = self.service.execute(self.run["id"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(self.service.execute(self.run["id"], verify_only=True)["status"], "pending")
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_concurrent_approval_and_execution_reuse_one_operation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            approvals = list(pool.map(lambda _: self.approve(), range(2)))
            results = list(pool.map(lambda _: self.service.execute(self.run["id"]), range(2)))
        self.assertEqual(approvals[0], approvals[1])
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_cli_approval_and_execution_do_not_construct_a_model_client(self):
        settings = Settings(self.path)
        for arguments, expected_status in [
            (["approve", self.run["id"], "--payment-id", "pi_1", "--amount", "500", "--currency", "usd"], "approved"),
            (["execute", self.run["id"]], "verified"),
            (["execution", self.run["id"]], "verified"),
            (["verify", self.run["id"]], "verified"),
        ]:
            output = io.StringIO()
            with patch("sys.argv", ["payresolve", "m2", *arguments]), \
                    patch("payresolve.__main__.Settings.from_environment", return_value=settings), \
                    patch("payresolve.m2.StripeSandbox", return_value=self.gateway), \
                    patch("payresolve.m2.GeminiExtractor") as model, redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 0)
                model.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["status"], expected_status)
        self.assertEqual(len(self.gateway.refund_posts), 1)


if __name__ == "__main__":
    unittest.main()
