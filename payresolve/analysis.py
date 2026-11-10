"""Persisted M2 dry runs. The model has no tools; policy can only retrieve Stripe."""

from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from .gemini_gateway import ExtractionError
from .intent import Intent, PAYMENT_ID, PROMPT_VERSION, amount_in_cents
from .operations import OperationStore
from .case_boundary import deny_linked_request
from .stripe_gateway import SandboxError

CLARIFICATION_QUESTIONS = {
    "ambiguous_request": "What refund do you want? Resolve any conditions or conflicting instructions.",
    "specify_one_payment_id": "Which single PaymentIntent (pi_...) should be refunded?",
    "specify_refund_amount_or_full": "What numeric amount should be refunded, or do you want the full remaining amount?",
    "specify_numeric_refund_amount": "What is the refund amount in numeric major currency units (for example, USD 5.00)?",
    "specify_currency": "What currency is the refund amount in? This prototype supports USD.",
    "conflicting_full_refund_amount": "Do you want a specific numeric amount or the full remaining amount? State one choice.",
}


def decision(action, code, **details):
    if action == "clarify":
        details["clarification"] = {
            "question": CLARIFICATION_QUESTIONS.get(code, "Please clarify the refund request."),
            "response_format": "complete_refund_request",
            "instructions": "Submit a complete corrected request with one PaymentIntent ID and either an amount with currency or an explicit full refund. Earlier text is preserved but not merged.",
        }
    return {"dry_run": True, "executed": False, "action": action, "code": code, **details}


class DryRunPolicy:
    def __init__(self, store, retrieve_payment):
        self.store = store
        # A read function, never the workflow or a refund-creation tool.
        self.retrieve_payment = retrieve_payment

    def assess(self, message, intent):
        if intent.intent == "other":
            return decision("no_action", "not_a_refund_request")
        if intent.intent != "refund" or intent.ambiguous:
            return decision("clarify", "ambiguous_request")
        literal_ids = set(PAYMENT_ID.findall(message))
        if len(literal_ids) != 1 or set(intent.payment_ids) != literal_ids:
            return decision("clarify", "specify_one_payment_id")
        payment_id = next(iter(literal_ids))
        if intent.refund_type == "unspecified":
            return decision("clarify", "specify_refund_amount_or_full")
        if intent.currency not in (None, "usd"):
            return decision("reject", "unsupported_currency")
        amount = None
        if intent.refund_type == "partial":
            amount = amount_in_cents(intent, message)
            if amount is None:
                return decision("clarify", "specify_numeric_refund_amount")
            if intent.currency is None or not re.search(r"\$|\bUSD\b|\b(?:US |U\.S\. )?dollars?\b", message, re.IGNORECASE):
                return decision("clarify", "specify_currency")
            if amount <= 0:
                return decision("reject", "invalid_refund_amount")
        elif intent.amount is not None or intent.amount_evidence is not None:
            return decision("clarify", "conflicting_full_refund_amount")
        return self.assess_payment(payment_id, amount)

    def assess_payment(self, payment_id, amount, currency="usd"):
        """Eligibility for a separately grounded, authorized structured proposal."""
        if currency != "usd":
            return decision("reject", "unsupported_currency")
        if amount is not None and (type(amount) is not int or amount <= 0):
            return decision("reject", "invalid_refund_amount")
        with self.store.connect() as connection:
            owner = connection.execute(
                "SELECT o.stripe_key FROM sandbox_payments p JOIN stripe_operations o "
                "ON p.operation_id = o.operation_id WHERE p.payment_id = ?", (payment_id,),
            ).fetchone()
            outstanding = connection.execute(
                "SELECT 1 FROM stripe_operations WHERE target = ? AND status IN ('unresolved', 'pending')",
                (payment_id,),
            ).fetchone()
        if not owner:
            return decision("reject", "unmanaged_payment")
        if outstanding:
            return decision("reject", "unresolved_operation")
        payment = self.retrieve_payment(payment_id)
        if (payment["id"] != payment_id or payment["livemode"] is not False
                or payment["operation_key"] != owner[0]):
            return decision("reject", "payment_identity_mismatch")
        if payment["status"] != "succeeded" or payment["paid"] is not True or payment["captured"] is not True:
            return decision("reject", "payment_not_refundable")
        if payment["disputed"] is not False:
            return decision("reject", "payment_disputed")
        if payment["currency"] != "usd":
            return decision("reject", "currency_mismatch")
        remaining = payment["refundable_amount"]
        if amount is None:
            amount = remaining
        if not 0 < amount <= remaining:
            return decision("reject", "invalid_refund_amount")
        return decision("propose_refund", "eligible_at_analysis_time", payment_id=payment_id,
                        amount_minor=amount, currency="usd", remaining_minor=remaining,
                        observed_at=datetime.now(timezone.utc).isoformat())


class AnalysisStore(OperationStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

    @staticmethod
    def new_analysis(request_id, model, **details):
        run = {"id": str(uuid4()), "request_id": request_id, "status": "started",
               "model": model, "prompt_version": PROMPT_VERSION,
               "dry_run": True, "executed": False,
               "created_at": datetime.now(timezone.utc).isoformat(), **details}
        return run

    @staticmethod
    def insert_analysis(connection, run):
        connection.execute("INSERT INTO analysis_runs VALUES (?, ?, ?, ?, ?)",
                           (run["id"], run["request_id"], run["status"], json.dumps(run), run["created_at"]))

    def start_analysis(self, request_id, model, *, message=None):
        run = self.new_analysis(request_id, model, input_message=message)
        with self.connect() as connection:
            deny_linked_request(connection, request_id)
            self.insert_analysis(connection, run)
        return run

    def start_clarification(self, parent_id, message, model):
        # Validate and commit the link atomically with respect to local approvals.
        with self.locked() as connection:
            row = connection.execute("SELECT * FROM analysis_runs WHERE id = ?", (parent_id,)).fetchone()
            if row is None:
                raise ExtractionError("analysis_not_found", "No analysis exists with that ID.")
            deny_linked_request(connection, row['request_id'])
            parent = json.loads(row["result_json"])
            if (row["status"] != "completed" or parent.get("status") != "completed"
                    or parent.get("decision", {}).get("action") != "clarify"):
                raise ExtractionError("clarification_not_requested", "Use an analysis whose completed decision asks for clarification.")
            has_approvals = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'refund_approvals'",
            ).fetchone()
            if has_approvals and connection.execute(
                "SELECT 1 FROM refund_approvals WHERE request_id = ?", (row["request_id"],),
            ).fetchone():
                raise ExtractionError("request_already_approved", "This request already has an approved analysis; inspect its execution instead.")
            run = self.new_analysis(row["request_id"], model, input_message=message,
                                    parent_analysis_id=parent_id, input_kind="clarification")
            self.insert_analysis(connection, run)
        return run

    def finish_analysis(self, run):
        with self.connect() as connection:
            connection.execute("UPDATE analysis_runs SET status = ?, result_json = ? WHERE id = ?",
                               (run["status"], json.dumps(run), run["id"]))
        return run

    def analysis(self, run_id):
        with self.connect() as connection:
            row = connection.execute("SELECT result_json, request_id FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
            if row:
                deny_linked_request(connection, row['request_id'])
        if not row:
            raise ExtractionError("analysis_not_found", "No analysis exists with that ID.")
        return json.loads(row[0])

    def history(self, request_id):
        request = self.get(request_id)
        if request is None:
            raise ExtractionError("request_not_found", "No request exists with that ID.")
        with self.connect() as connection:
            runs = [json.loads(row[0]) for row in connection.execute(
                "SELECT result_json FROM analysis_runs WHERE request_id = ? ORDER BY created_at, rowid", (request_id,),
            )]
        return {"request": request, "analyses": runs}


class AnalysisService:
    def __init__(self, store, extractor, policy):
        self.store, self.extractor, self.policy = store, extractor, policy

    def analyze(self, *, message=None, request_id=None):
        if request_id is not None:
            request = self.store.get(request_id)
            if request is None:
                raise ExtractionError("request_not_found", "No request exists with that ID.")
            message = request["message"]
        message = self.extractor.validate(message)
        if request_id is None:
            request_id = self.store.create(message)["id"]
        run = self.store.start_analysis(request_id, self.extractor.model, message=message)
        return self._process(run, message)

    def clarify(self, parent_id, message):
        message = self.extractor.validate(message)
        run = self.store.start_clarification(parent_id, message, self.extractor.model)
        return self._process(run, message)

    def _process(self, run, message):
        try:
            extracted = self.extractor.extract(message)
            run.update(extracted)
            run["decision"] = self.policy.assess(message, Intent.model_validate(extracted["intent"]))
            run["status"] = "completed"
        except (ExtractionError, SandboxError) as error:
            run.update(status="failed", error={"code": error.code, "message": error.message})
            if isinstance(error, ExtractionError):
                run.update(attempts=error.attempts, latency_ms=error.latency_ms)
                if hasattr(error, "attempt_history"):
                    run["attempt_history"] = error.attempt_history
        return self.store.finish_analysis(run)
