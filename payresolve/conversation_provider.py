"""Persistent simulated payments for conversation trials; no Stripe client."""

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .recovery_provider import RecoveryProvider
from .stripe_gateway import SandboxError
from .workflow import SandboxWorkflow


class EvaluationOrder(BaseModel):
    """Synthetic initial state only; never used by the live catalog seeder."""
    model_config = ConfigDict(extra='forbid', strict=True)
    order_id: str = Field(pattern=r'^ORD-[0-9]{1,12}$')
    customer: str = Field(pattern=r'^[a-z][a-z0-9-]{0,31}$')
    description: str = Field(min_length=1, max_length=200)
    purchased_on: str
    amount_minor: int = Field(ge=50, le=1000000)
    payment_state: Literal['succeeded', 'unpaid', 'uncaptured'] = 'succeeded'
    captured_minor: int | None = Field(default=None, ge=0, le=99999999)
    refunded_minor: int = Field(default=0, ge=0, le=99999999)
    disputed: bool = False

    @model_validator(mode='after')
    def valid_state(self):
        if date.fromisoformat(self.purchased_on).isoformat() != self.purchased_on:
            raise ValueError('Use an ISO purchase date.')
        captured = self.capture_amount()
        if captured > self.amount_minor or self.refunded_minor > captured:
            raise ValueError('Capture/refund exceeds the initial balance.')
        if self.payment_state != 'succeeded' and (captured or self.refunded_minor):
            raise ValueError('Unpaid/uncaptured fixtures cannot have captured or refunded funds.')
        if self.payment_state == 'succeeded' and captured == 0:
            raise ValueError('Use uncaptured for a zero-capture payment.')
        return self

    def capture_amount(self):
        return self.captured_minor if self.captured_minor is not None else (
            self.amount_minor if self.payment_state == 'succeeded' else 0)


class EvaluationFixture(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    orders: list[EvaluationOrder] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def unique_orders(self):
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise ValueError('Fixture order IDs must be unique.')
        return self


class ConversationProvider(RecoveryProvider):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS evaluation_controls (name TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def set_control(self, name, value):
        allowed = {'fault': {'before_refund', 'after_refund', 'refund_read', 'payment_read'},
                   'refund_status': {'succeeded', 'pending', 'failed'}}
        if name not in allowed or value not in allowed[name]:
            raise ValueError('Unsupported evaluation control.')
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO evaluation_controls VALUES (?,?)', (name, value))

    def _fail(self, point):
        super()._fail(point)
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT value FROM evaluation_controls WHERE name='fault'").fetchone()
            triggered = row is not None and row[0] == point
            if triggered:
                connection.execute("DELETE FROM evaluation_controls WHERE name='fault'")
        if triggered:
            raise SandboxError('stripe_connection_failed', 'Injected persistent evaluation failure.')

    def create_refund(self, params, key):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM evaluation_controls WHERE name='refund_status'").fetchone()
        if row:
            self.next_refund_status = row[0]
        return super().create_refund(params, key)

    def external_refund_for(self, payment_id, amount, key):
        return self._record_refund(dict(payment_intent=payment_id, amount=amount,
                                       metadata={'payresolve_operation': key}),
                                   key, source='external', status='succeeded')

    def dispute_payment(self, payment_id):
        with self.connect() as connection:
            row = connection.execute('SELECT data_json FROM payments WHERE id=?', (payment_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown evaluation payment.')
            payment = json.loads(row[0])
            payment['disputed'] = True
            connection.execute('UPDATE payments SET data_json=? WHERE id=?', (json.dumps(payment), payment_id))

    def settle_refund(self, refund_id):
        with self.connect() as connection:
            row = connection.execute('SELECT data_json FROM refunds WHERE id=?', (refund_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown evaluation refund.')
            refund = json.loads(row[0])
            if refund['status'] not in ('pending', 'succeeded'):
                raise ValueError('Only a pending refund can settle successfully.')
            refund['status'] = 'succeeded'
            connection.execute('UPDATE refunds SET data_json=? WHERE id=?', (json.dumps(refund), refund_id))

    def seed_fixture(self, store, fixture):
        """Install an isolated evaluation catalog once, preserving restart state.

        Managed payment creation uses the real workflow. Initial provider states
        and the completion marker commit together, so reopening cannot reset a
        consumed balance or mistake a new refund for a preexisting one.
        """
        canonical = json.dumps(fixture.model_dump(), sort_keys=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS evaluation_fixture '
                               '(id INTEGER PRIMARY KEY CHECK(id=1), specification TEXT, baseline TEXT)')
            saved = connection.execute('SELECT specification,baseline FROM evaluation_fixture WHERE id=1').fetchone()
        if saved:
            if saved['specification'] != canonical:
                raise ValueError('Evaluation fixture changed after initialization.')
            return json.loads(saved['baseline'])
        with store.locked() as connection:
            connection.execute('INSERT OR IGNORE INTO demo_catalogs VALUES (?,?,?)',
                               ('conversation', '2026-11-05', 'evaluation-custom-v1'))
            version = connection.execute('SELECT fixture_version FROM demo_catalogs WHERE namespace=?',
                                         ('conversation',)).fetchone()[0]
            if version != 'evaluation-custom-v1':
                raise ValueError('Custom fixtures require an isolated evaluation catalog.')
            for customer in sorted({order.customer for order in fixture.orders}):
                connection.execute('INSERT OR IGNORE INTO demo_customers VALUES (?,?,?)',
                                   (f'conversation:{customer}', 'conversation', f'Evaluation customer {customer}'))
        payments = []
        workflow = SandboxWorkflow(store, self)
        for order in fixture.orders:
            operation_id = f'evaluation-{order.order_id}'
            result = workflow.create_payment(operation_id, order.amount_minor)
            if result['status'] != 'verified':
                raise ValueError('Evaluation payment seed was not verified.')
            payment = result['payment'].copy()
            values = ('conversation', order.order_id, f'conversation:{order.customer}',
                      order.description, order.purchased_on, order.amount_minor, 'usd', payment['id'], operation_id)
            with store.locked() as connection:
                existing = connection.execute('SELECT * FROM customer_orders WHERE namespace=? AND order_id=?',
                                              ('conversation', order.order_id)).fetchone()
                if existing and tuple(existing) != values:
                    raise ValueError('Evaluation order changed during initialization.')
                connection.execute('INSERT OR IGNORE INTO customer_orders VALUES (?,?,?,?,?,?,?,?,?)', values)
            payments.append((order, payment))
        baseline = {'refund_ids': [], 'post_ids': []}
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM refunds UNION ALL SELECT 1 FROM posts LIMIT 1').fetchone():
                raise ValueError('Fixture initialization requires an unused provider ledger.')
            for order, payment in payments:
                payment.update(status={'succeeded': 'succeeded', 'unpaid': 'requires_payment_method',
                                       'uncaptured': 'requires_capture'}[order.payment_state],
                               paid=order.payment_state != 'unpaid', captured=order.payment_state == 'succeeded',
                               amount_captured=order.capture_amount(), disputed=order.disputed)
                # snapshot() derives balances from refunds; do not store stale derived fields.
                for key in ('amount_refunded', 'refundable_amount'):
                    payment.pop(key, None)
                connection.execute('UPDATE payments SET data_json=? WHERE id=?', (json.dumps(payment), payment['id']))
                if order.refunded_minor:
                    refund_id = f're_initial_{order.order_id}'
                    key = f'initial-{order.order_id}'
                    params = dict(payment_intent=payment['id'], amount=order.refunded_minor,
                                  metadata={'payresolve_operation': key})
                    refund = dict(id=refund_id, payment_intent=payment['id'], charge=payment['charge_id'],
                                  amount=order.refunded_minor, currency='usd', status='succeeded', operation_key=key)
                    connection.execute('INSERT INTO refunds VALUES (?,?,?,?,?)',
                                       (refund_id, key, json.dumps(params, sort_keys=True), json.dumps(refund), 'external'))
                    baseline['refund_ids'].append(refund_id)
            connection.execute('INSERT INTO evaluation_fixture VALUES (1,?,?)', (canonical, json.dumps(baseline)))
        return baseline

    def create_payment(self, params, key):
        suffix = hashlib.sha256(key.encode()).hexdigest()[:20]
        paid = params.get('confirm', False)
        payment = dict(id='pi_sim' + suffix, amount=params['amount'], currency=params['currency'],
                       status='succeeded' if paid else 'requires_payment_method', livemode=False,
                       operation_key=params['metadata']['payresolve_operation'], charge_id='ch_sim' + suffix,
                       paid=paid, captured=paid, disputed=False,
                       amount_captured=params['amount'] if paid else 0)
        with self.connect() as c:
            existing = c.execute('SELECT data_json FROM payments WHERE id=?', (payment['id'],)).fetchone()
            if existing and json.loads(existing[0]) != payment:
                raise SandboxError('provider_payment_conflict', 'Simulation fixture differs.')
            c.execute('INSERT OR IGNORE INTO payments VALUES (?,?)', (payment['id'], json.dumps(payment)))
        return payment['id']
