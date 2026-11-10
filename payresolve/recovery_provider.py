"""Local fault-injectable provider for workflow experiments; never accesses Stripe."""

from contextlib import contextmanager
import json
import sqlite3

from .stripe_gateway import SandboxError


class RecoveryProvider:
    def __init__(self, path):
        self.path = path
        self.fault = None
        self.next_refund_status = "succeeded"
        self.before_post = lambda params, key: None
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refunds (id TEXT PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL,
                    params_json TEXT NOT NULL, data_json TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL, params_json TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def check_connection(self):
        return {"status": "connected", "livemode": False}

    def create_payment(self, params, key):
        paid = params.get("confirm", False)
        payment = dict(id="pi_recovery", amount=params["amount"], currency=params["currency"],
                       status="succeeded" if paid else "requires_payment_method", livemode=False,
                       operation_key=key, charge_id="ch_recovery", paid=paid, captured=paid,
                       disputed=False, amount_captured=params["amount"] if paid else 0)
        with self.connect() as connection:
            existing = connection.execute("SELECT data_json FROM payments WHERE id='pi_recovery'").fetchone()
            if existing and json.loads(existing[0]) != payment:
                raise SandboxError("provider_payment_conflict", "Synthetic fixture already exists.")
            connection.execute("INSERT OR IGNORE INTO payments VALUES (?,?)", (payment["id"], json.dumps(payment)))
        return payment["id"]

    def fail_once(self, fault):
        self.fault = fault

    def _fail(self, point):
        if self.fault == point:
            self.fault = None
            raise SandboxError("stripe_connection_failed", "Injected local provider failure.")

    def snapshot(self):
        with self.connect() as connection:
            payments = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM payments")]
            refunds = [json.loads(row["data_json"]) | {"source": row["source"]}
                       for row in connection.execute("SELECT * FROM refunds ORDER BY rowid")]
            posts = [dict(row) for row in connection.execute("SELECT * FROM posts ORDER BY id")]
        for payment in payments:
            payment["amount_refunded"] = sum(refund["amount"] for refund in refunds
                                             if refund["payment_intent"] == payment["id"] and refund["status"] == "succeeded")
            payment["refundable_amount"] = payment["amount_captured"] - payment["amount_refunded"]
        return {"payments": payments, "refunds": refunds, "posts": posts}

    def get_payment(self, payment_id):
        snapshot = self.snapshot()
        if snapshot["refunds"]:
            self._fail("payment_read")
        for payment in snapshot["payments"]:
            if payment["id"] == payment_id:
                return payment
        raise SandboxError("provider_payment_missing", "Synthetic payment missing.")

    def create_refund(self, params, key):
        self.before_post(params, key)
        with self.connect() as connection:
            connection.execute("INSERT INTO posts (operation_key,params_json) VALUES (?,?)",
                               (key, json.dumps(params, sort_keys=True)))
        self._fail("before_refund")
        refund_id = self._record_refund(params, key, source="workflow", status=self.next_refund_status)
        self._fail("after_refund")
        return refund_id

    def _record_refund(self, params, key, *, source, status):
        canonical = json.dumps(params, sort_keys=True)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM refunds WHERE operation_key=?", (key,)).fetchone()
            if existing:
                if existing["params_json"] != canonical:
                    raise SandboxError("provider_idempotency_conflict", "Synthetic idempotency conflict.")
                return existing["id"]
            row = connection.execute("SELECT data_json FROM payments WHERE id=?", (params["payment_intent"],)).fetchone()
            if row is None:
                raise SandboxError("provider_payment_missing", "Synthetic payment missing.")
            payment = json.loads(row[0])
            refunds = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM refunds")]
            reserved = sum(refund["amount"] for refund in refunds if refund["payment_intent"] == payment["id"]
                           and refund["status"] in ("succeeded", "pending"))
            amount = params["amount"]
            if (type(amount) is not int or amount <= 0 or amount > payment["amount_captured"] - reserved
                    or not payment["paid"] or not payment["captured"] or payment["livemode"]):
                raise SandboxError("provider_invalid_refund", "Synthetic refund rejected.")
            refund_id = f"re_recovery{len(refunds) + 1}"
            refund = dict(id=refund_id, payment_intent=payment["id"], charge=payment["charge_id"],
                          amount=amount, currency=payment["currency"], status=status,
                          operation_key=params["metadata"]["payresolve_operation"])
            connection.execute("INSERT INTO refunds VALUES (?,?,?,?,?)",
                               (refund_id, key, canonical, json.dumps(refund), source))
        return refund_id

    def get_refund(self, refund_id):
        self._fail("refund_read")
        with self.connect() as connection:
            row = connection.execute("SELECT data_json FROM refunds WHERE id=?", (refund_id,)).fetchone()
        if row is None:
            raise SandboxError("provider_refund_missing", "Synthetic refund missing.")
        return json.loads(row[0])

    def settle_pending(self):
        with self.connect() as connection:
            for row in connection.execute("SELECT id,data_json FROM refunds").fetchall():
                refund = json.loads(row["data_json"])
                if refund["status"] == "pending":
                    refund["status"] = "succeeded"
                    connection.execute("UPDATE refunds SET data_json=? WHERE id=?", (json.dumps(refund), row["id"]))

    def dispute(self):
        with self.connect() as connection:
            payment = json.loads(connection.execute("SELECT data_json FROM payments").fetchone()[0])
            payment["disputed"] = True
            connection.execute("UPDATE payments SET data_json=? WHERE id=?", (json.dumps(payment), payment["id"]))

    def external_refund(self, amount):
        return self._record_refund(dict(payment_intent="pi_recovery", amount=amount,
                                       metadata={"payresolve_operation": "external"}),
                                   "external", source="external", status="succeeded")
