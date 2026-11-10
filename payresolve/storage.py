"""Small SQLite repository. Each operation owns and closes its connection."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .case_boundary import deny_linked_request


class RequestStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS refund_requests (
                    id TEXT PRIMARY KEY,
                    message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 4000),
                    status TEXT NOT NULL DEFAULT 'received' CHECK(status = 'received'),
                    created_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def check(self):
        with self.connect() as connection:
            connection.execute("SELECT id FROM refund_requests LIMIT 1").fetchone()

    def create(self, message: str) -> dict:
        record = {
            "id": str(uuid4()), "message": message, "status": "received",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO refund_requests (id, message, status, created_at) "
                "VALUES (:id, :message, :status, :created_at)", record,
            )
        return record

    def get(self, request_id: str) -> dict | None:
        with self.connect() as connection:
            deny_linked_request(connection, request_id)
            row = connection.execute(
                "SELECT id, message, status, created_at FROM refund_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None
