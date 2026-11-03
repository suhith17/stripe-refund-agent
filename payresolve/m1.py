"""Explicit local CLI for controlled M1 sandbox experiments."""

import re

from .operations import OperationStore
from .stripe_gateway import SandboxError, StripeSandbox
from .workflow import SandboxWorkflow, reject


def add_parser(subparsers):
    parser = subparsers.add_parser("m1", help="Controlled sandbox payment/refund operations")
    commands = parser.add_subparsers(dest="m1_command", required=True)
    create = commands.add_parser("create-payment", help="Create a USD test payment using pm_card_visa")
    create.add_argument("--operation-id", required=True)
    create.add_argument("--amount", required=True, type=int, help="USD cents, e.g. 3999")
    create.add_argument("--unpaid", action="store_true", help="Create an unconfirmed fixture without a payment method")
    get = commands.add_parser("get-payment", help="Retrieve fresh payment/charge state")
    get.add_argument("payment_id")
    commands.add_parser("list-payments", help="List locally stored payment snapshots")
    refund = commands.add_parser("refund", help="Refund a locally created sandbox payment")
    refund.add_argument("--operation-id", required=True)
    refund.add_argument("--payment-id", required=True)
    refund.add_argument("--amount", type=int, help="USD cents; omitted = full remaining amount")
    refund.add_argument("--currency", default="usd")
    for command, help_text in [("operation", "Inspect the local operation journal"),
                               ("verify", "Read-only verification of a known Stripe operation")]:
        item = commands.add_parser(command, help=help_text)
        item.add_argument("operation_id")
    fixtures = commands.add_parser("fixtures", help="Create four fixtures, exercise two refunds, and verify final state")
    fixtures.add_argument("--run-id", required=True, help="Reuse this ID on retry; a new ID creates new fixtures")


def execute(args, settings):
    # Validate the key before initializing storage, even for commands that may write.
    gateway = StripeSandbox(settings.stripe_secret_key)
    workflow = SandboxWorkflow(OperationStore(settings.database_path), gateway)
    if args.m1_command == "create-payment":
        return workflow.create_payment(args.operation_id, args.amount, unpaid=args.unpaid)
    if args.m1_command == "get-payment":
        return workflow.get_payment(args.payment_id)
    if args.m1_command == "list-payments":
        return {"payments": workflow.store.list_payments(), "source": "local_snapshots"}
    if args.m1_command == "refund":
        return workflow.refund(args.operation_id, args.payment_id, args.amount, currency=args.currency)
    if args.m1_command == "operation":
        return workflow.operation(args.operation_id)
    if args.m1_command == "verify":
        return workflow.verify(args.operation_id)
    return run_fixtures(workflow, args.run_id)


def run_fixtures(workflow, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", run_id):
        reject("invalid_run_id", "Use 1 to 60 letters, digits, underscores or hyphens for the run ID.")
    payments = {}
    for name, amount, unpaid in [("unrefunded", 3999, False), ("partial", 1500, False),
                                 ("full", 2000, False), ("unpaid", 1000, True)]:
        created = workflow.create_payment(f"{run_id}-{name}", amount, unpaid=unpaid)
        if created["status"] != "verified":
            reject("fixture_incomplete", "A payment fixture is not verified. Inspect its operation before continuing.")
        payments[name] = created["payment"]["id"]
    partial = workflow.refund(f"{run_id}-partial-refund", payments["partial"], 500)
    full = workflow.refund(f"{run_id}-full-refund", payments["full"])
    if partial["status"] != "verified" or full["status"] != "verified":
        reject("fixture_incomplete", "A refund is not yet verified. Inspect its operation and retry this run ID.")
    # Reopen storage to exercise durable replay, as on a subsequent CLI invocation.
    reopened = SandboxWorkflow(OperationStore(workflow.store.path), workflow.gateway)
    replay = reopened.refund(f"{run_id}-partial-refund", payments["partial"], 500)
    if partial["refund"]["id"] != replay["refund"]["id"]:
        reject("fixture_failed", "Refund replay returned a different Stripe ID.")
    checks = {"persistent_replay": "passed"}
    for label, payment, amount, expected_error in [
        ("zero_amount", payments["partial"], 0, "invalid_refund_amount"),
        ("over_refund", payments["partial"], 1001, "invalid_refund_amount"),
        ("already_refunded", payments["full"], None, "invalid_refund_amount"),
        ("unpaid", payments["unpaid"], None, "payment_not_refundable"),
    ]:
        try:
            workflow.refund(f"{run_id}-reject-{label}", payment, amount)
        except SandboxError as error:
            if error.code != expected_error:
                raise
            checks[label] = "passed"
        else:
            reject("fixture_failed", "An invalid refund was not rejected.")
    snapshots = {name: workflow.get_payment(payment_id) for name, payment_id in payments.items()}
    for name, expected in [("unrefunded", 0), ("partial", 500), ("full", 2000), ("unpaid", 0)]:
        if snapshots[name]["amount_refunded"] != expected:
            reject("fixture_failed", "Final Stripe state differs from the fixture expectation.")
    return {"run_id": run_id, "status": "verified", "payments": snapshots,
            "refund_ids": {"partial": partial["refund"]["id"], "full": full["refund"]["id"]}, "checks": checks}
