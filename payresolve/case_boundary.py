"""Shared guards so operator entry points cannot bypass customer case claims."""

from .stripe_gateway import SandboxError


def enabled(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refund_cases'").fetchone() is not None


def deny_linked_request(connection, request_id):
    if enabled(connection) and connection.execute("SELECT 1 FROM refund_cases WHERE request_id=?", (request_id,)).fetchone():
        raise SandboxError("case_access_required", "Open this request through its customer case.", 403)


def authorize_operation(connection, operation_id, customer_id=None, target=None, amount=None, currency=None):
    if not enabled(connection):
        return
    case = connection.execute("SELECT * FROM refund_cases WHERE operation_id=?", (operation_id,)).fetchone()
    if case:
        proposal = connection.execute("SELECT * FROM case_proposals WHERE case_id=? AND revision=?",
                                      (case['id'], case['confirmed_revision'])).fetchone()
        owner = connection.execute("SELECT 1 FROM customer_orders WHERE customer_id=? AND payment_id=?",
                                   (customer_id, proposal['payment_id'] if proposal else None)).fetchone()
        if (case['customer_id'] != customer_id or not owner or not proposal
                or case['revision'] != case['confirmed_revision'] or case['stage'] == 'cancelled'
                or (target is not None and (proposal['payment_id'] != target
                    or proposal['amount_minor'] != amount or type(amount) is not int
                    or proposal['currency'] != currency))):
            raise SandboxError("case_access_required", "Use the confirmed operation through its customer case.", 403)
    elif target and connection.execute("SELECT 1 FROM case_proposals WHERE payment_id=?", (target,)).fetchone():
        raise SandboxError("case_confirmation_required", "This purchase requires a current customer case confirmation.", 403)
