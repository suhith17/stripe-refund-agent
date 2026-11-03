"""Explicit local approval of a fixed proposal, followed by M1 execution."""

from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from .analysis import AnalysisStore
from .stripe_gateway import SandboxError
from .workflow import SandboxWorkflow, reject


class ExecutionStore(AnalysisStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS refund_approvals (
                    request_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL UNIQUE,
                    payment_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL CHECK(currency = 'usd'),
                    approved_at TEXT NOT NULL,
                    last_error TEXT
                )
            """)

    def approve(self, analysis_id, payment_id, amount, currency):
        if (not isinstance(payment_id, str) or not re.fullmatch(r"pi_[A-Za-z0-9]+", payment_id)
                or type(amount) is not int or amount <= 0 or currency != "usd"):
            reject("invalid_approval", "Confirm the proposal's PaymentIntent, positive integer USD cents, and usd currency.")
        with self.locked() as connection:
            row = connection.execute("SELECT * FROM analysis_runs WHERE id = ?", (analysis_id,)).fetchone()
            if row is None:
                reject("analysis_not_found", "No analysis exists with that ID.")
            run = json.loads(row["result_json"])
            proposal = run.get("decision", {})
            if (row["status"] != "completed" or run.get("status") != "completed"
                    or proposal.get("action") != "propose_refund"
                    or proposal.get("dry_run") is not True or proposal.get("executed") is not False):
                reject("proposal_not_approvable", "Only a completed refund proposal can be approved.")
            if (proposal.get("payment_id") != payment_id or type(proposal.get("amount_minor")) is not int
                    or proposal["amount_minor"] != amount or proposal.get("currency") != currency):
                reject("approval_mismatch", "The confirmed payment, amount, or currency differs from the saved proposal.")
            existing = connection.execute("SELECT * FROM refund_approvals WHERE request_id = ?", (row["request_id"],)).fetchone()
            if existing:
                if existing["analysis_id"] != analysis_id:
                    reject("request_already_approved", "This intake request already has an approved analysis. Use its original analysis ID.")
                if (existing["payment_id"], existing["amount_minor"], existing["currency"]) != (payment_id, amount, currency):
                    reject("approval_mismatch", "This approval is immutable. Use its original parameters.")
                return dict(existing)
            approval = {"request_id": row["request_id"], "analysis_id": analysis_id,
                        "operation_id": "m2_" + uuid4().hex, "payment_id": payment_id,
                        "amount_minor": amount, "currency": currency,
                        "approved_at": datetime.now(timezone.utc).isoformat(), "last_error": None}
            connection.execute(
                "INSERT INTO refund_approvals VALUES (:request_id, :analysis_id, :operation_id, :payment_id, "
                ":amount_minor, :currency, :approved_at, :last_error)", approval,
            )
        return approval

    def approval(self, analysis_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM refund_approvals WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if row is None:
            reject("approval_required", "Review and approve this analysis before execution.")
        return dict(row)

    def execution(self, analysis_id):
        approval = self.approval(analysis_id)
        with self.connect() as connection:
            operation = self.operation(connection, approval["operation_id"])
        return {"approval": approval, "status": operation["status"] if operation else "approved",
                "stripe_id": operation["stripe_id"] if operation else None,
                "result": json.loads(operation["result_json"]) if operation and operation["result_json"] else None}


class ReviewedExecution:
    def __init__(self, store, gateway):
        self.store = store
        self.workflow = SandboxWorkflow(store, gateway)

    def execute(self, analysis_id, *, verify_only=False):
        approval = self.store.approval(analysis_id)
        try:
            if verify_only:
                self.workflow.verify(approval["operation_id"])
            else:
                # Always use the approved numeric amount, even for "full". M1
                # revalidates fresh payment state before preparing the first POST.
                # Retrying reuses the persisted operation and Stripe key.
                self.workflow.refund(approval["operation_id"], approval["payment_id"],
                                     approval["amount_minor"], currency=approval["currency"])
        except SandboxError as error:
            with self.store.connect() as connection:
                connection.execute("UPDATE refund_approvals SET last_error = ? WHERE analysis_id = ?",
                                   (error.code, analysis_id))
            raise
        with self.store.connect() as connection:
            connection.execute("UPDATE refund_approvals SET last_error = NULL WHERE analysis_id = ?", (analysis_id,))
        return self.store.execution(analysis_id)
