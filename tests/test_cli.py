from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from payresolve.__main__ import main
from payresolve.config import Settings
from tests.test_workflow import FakeStripe


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(Path(directory.name) / "test.db")
        self.gateway = FakeStripe()

    def run_cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["payresolve", "m1", *arguments]), \
                patch("payresolve.__main__.Settings.from_environment", return_value=self.settings), \
                patch("payresolve.m1.StripeSandbox", return_value=self.gateway), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main()
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_cli_create_refund_replay_and_read_only_verify(self):
        code, created = self.run_cli("create-payment", "--operation-id", "payment", "--amount", "1000")
        self.assertEqual(code, 0)
        payment_id = created["payment"]["id"]
        args = ("refund", "--operation-id", "refund", "--payment-id", payment_id, "--amount", "250")
        first = self.run_cli(*args)
        self.assertEqual(first[0], 0)
        self.assertEqual(self.run_cli(*args), first)
        self.assertEqual(self.run_cli("verify", "refund"), first)
        self.assertEqual(len(self.gateway.refund_posts), 1)

    def test_cli_invalid_amount_returns_sanitized_error_and_failure(self):
        code, result = self.run_cli("create-payment", "--operation-id", "bad", "--amount", "0")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "invalid_payment")

    def test_cli_pending_has_distinct_exit_status(self):
        _, created = self.run_cli("create-payment", "--operation-id", "payment", "--amount", "1000")
        self.gateway.refund_status = "pending"
        code, result = self.run_cli("refund", "--operation-id", "refund", "--payment-id", created["payment"]["id"])
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "pending")
