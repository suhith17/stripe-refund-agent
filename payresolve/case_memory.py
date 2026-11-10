"""Source-backed partial details; no authority to propose or execute refunds."""

import json
import re

from .agent_contract import ground_order, ground_amount, ground_currency, guard_latest_request, pause_requested, full_refund_requested
from .workflow import reject
from .order_dates import date_constraints


def read_memory(connection, case_id):
    row = connection.execute('SELECT data_json FROM case_memory WHERE case_id=?', (case_id,)).fetchone()
    return json.loads(row[0]) if row else {'facts': {}, 'barriers': {}}


def write_memory(connection, case_id, memory):
    connection.execute('INSERT INTO case_memory VALUES (?,?) ON CONFLICT(case_id) DO UPDATE SET data_json=excluded.data_json',
                       (case_id, json.dumps(memory)))


def invalidate(connection, case_id, message_id, text):
    """Conservative recognition of edits; old source IDs cannot revive cleared facts."""
    memory = read_memory(connection, case_id)
    fields = set()
    if date_constraints(text):
        fields.add('order')
    if re.search(r'\bORD-[\w-]+|\bpi_\w+|\b\d{4}-\d{2}-\d{2}\b|\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b|\b(?:other|different)\s+(?:one|order|purchase)\b', text, re.I):
        fields.add('order')
    if full_refund_requested(text) or re.search(r'\$\s*\d|\b(?:USD|EUR|GBP)\s*\d|\b(?:refund|make|change|actually|instead|only|just)\b.*\d|^\s*\d+(?:\.\d+)?\s*$', text, re.I):
        fields.add('amount')
    if re.search(r'\$|[€£]|\b(?:USD|EUR|GBP|euros?|pounds?|US dollars?)\b', text, re.I):
        fields.add('currency')
    if pause_requested(text):
        fields.update(('amount', 'currency'))
    if re.search(r'[€£]|\b(?:EUR|GBP|euros?|pounds?)\b', text, re.I):
        fields.add('amount')
    removed = sorted(fields & memory['facts'].keys())
    for field in fields:
        memory['facts'].pop(field, None)
        memory['barriers'][field] = message_id
    if fields:
        write_memory(connection, case_id, memory)
    return removed


def remember(connection, case, action, catalog, customer):
    memory = read_memory(connection, case['id'])
    facts = {}
    if any((action.order_id, action.order_message_id, action.order_evidence)):
        order = ground_order(action, case, catalog, customer)
        facts['order'] = {'order_id': order['order_id'], 'message_id': action.order_message_id, 'evidence': action.order_evidence}
    if any((action.refund_type, action.amount, action.amount_message_id, action.amount_evidence)):
        guard_latest_request(case)
        if action.refund_type not in ('partial', 'full'):
            reject('invalid_memory', 'Specify whether the remembered amount is partial or full.')
        amount = ground_amount(action, case)
        facts['amount'] = {'refund_type': action.refund_type, 'amount': action.amount, 'amount_minor': amount,
                           'message_id': action.amount_message_id, 'evidence': action.amount_evidence}
    if any((action.currency_message_id, action.currency_evidence)):
        guard_latest_request(case)
        ground_currency(action, case)
        facts['currency'] = {'currency': 'usd', 'message_id': action.currency_message_id, 'evidence': action.currency_evidence}
    positions = {m['id']: i for i, m in enumerate(case['messages'])}
    for field, fact in facts.items():
        barrier = memory['barriers'].get(field)
        if barrier and positions[fact['message_id']] < positions[barrier]:
            if field == 'order':
                latest = case['messages'][positions[barrier]]
                ids = re.findall(r'\b(?:ORD-[\w-]+|pi_\w+)\b', latest['content'], re.I)
                # Repeated explicit selection is fresh evidence of the same
                # order, not a reason to ask the customer to select it again.
                # Validate the whole message so conflicting IDs/dates still fail.
                if latest['role'] == 'customer' and any(
                        ref.lower() == action.order_id.lower() for ref in ids):
                    refreshed = action.model_copy(update={
                        'order_message_id': latest['id'],
                        'order_evidence': latest['content'],
                    })
                    ground_order(refreshed, case, catalog, customer)
                    fact.update(message_id=latest['id'], evidence=latest['content'])
                    continue
            reject('stale_memory_evidence', 'Use source evidence from the latest correction or clarify again.')
    # All groups validate before committing any of this action's claims.
    changed = {field: fact for field, fact in facts.items() if memory['facts'].get(field) != fact}
    memory['facts'].update(facts)
    if changed:
        write_memory(connection, case['id'], memory)
    return changed
