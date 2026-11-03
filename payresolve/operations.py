"""Durable operation journal for the local, serialized M1 workflow."""

from contextlib import contextmanager
import json

from .storage import RequestStore


class OperationStore(RequestStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS stripe_operations (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('payment', 'refund')),
                    target TEXT,
                    input_json TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    stripe_key TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    stripe_id TEXT,
                    status TEXT NOT NULL DEFAULT 'unresolved'
                        CHECK(status IN ('unresolved', 'pending', 'verified', 'failed')),
                    result_json TEXT,
                    last_error TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sandbox_payments (
                    payment_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL
                )
            """)

    @contextmanager
    def locked(self):
        # Serialize local workflows across threads/processes, including network I/O.
        # This is intentionally a small single-user prototype, not a job queue.
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def operation(connection, operation_id):
        row = connection.execute(
            "SELECT * FROM stripe_operations WHERE operation_id = ?", (operation_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def save_payment(connection, operation_id, payment):
        connection.execute(
            "INSERT INTO sandbox_payments VALUES (?, ?, ?) "
            "ON CONFLICT(payment_id) DO UPDATE SET snapshot_json = excluded.snapshot_json",
            (payment["id"], operation_id, json.dumps(payment, sort_keys=True)),
        )

    def list_payments(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT snapshot_json FROM sandbox_payments ORDER BY payment_id")]
