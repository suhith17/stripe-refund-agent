"""Persistent customer cases and immutable proposal revisions; no model decisions."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from .analysis import DryRunPolicy
from .catalog import CatalogStore
from .execution import ExecutionStore, ReviewedExecution
from .gemini_gateway import validate_message
from .stripe_gateway import SandboxError
from .workflow import reject
from .case_memory import read_memory, invalidate, remember
from .agent_contract import AgentAction


def now():
    return datetime.now(timezone.utc).isoformat()


class CaseStore(ExecutionStore):
    def __init__(self, path):
        super().__init__(path)
        self.catalog = CatalogStore(path)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS case_memory (case_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)')
            c.execute("""CREATE TABLE IF NOT EXISTS refund_cases (
                id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 0, selected_order TEXT,
                amount_minor INTEGER, currency TEXT, candidates_json TEXT NOT NULL DEFAULT '[]',
                question TEXT, confirmed_revision INTEGER, operation_id TEXT UNIQUE,
                refund_id TEXT, last_error TEXT, observed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS case_messages (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                kind TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS case_proposals (
                case_id TEXT NOT NULL, revision INTEGER NOT NULL, analysis_id TEXT NOT NULL UNIQUE,
                order_id TEXT NOT NULL, payment_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                currency TEXT NOT NULL, source_message_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(case_id, revision))""")
            c.execute("""CREATE TABLE IF NOT EXISTS agent_turns (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, case_version INTEGER NOT NULL,
                status TEXT NOT NULL, context_mode TEXT NOT NULL, model TEXT NOT NULL,
                expires_at REAL NOT NULL, model_steps INTEGER NOT NULL,
                tool_calls INTEGER NOT NULL, created_at TEXT NOT NULL)""")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn ON agent_turns(case_id) WHERE status='running'")

    @staticmethod
    def owned(c, customer_id, case_id):
        row = c.execute("SELECT * FROM refund_cases WHERE id=? AND customer_id=?", (case_id, customer_id)).fetchone()
        if row is None:
            raise SandboxError('case_not_found', 'No case was found for this demo customer.', 404)
        return dict(row)

    @staticmethod
    def editable(case, version):
        if type(version) is not int or case['version'] != version:
            reject('stale_case', 'This case changed. Reload it before continuing.')
        if case['operation_id'] or case['stage'] == 'cancelled':
            reject('case_locked', 'This case cannot be changed. Reconcile its existing operation first.')

    @staticmethod
    def event(c, case_id, kind, details):
        c.execute('INSERT INTO case_events(case_id,kind,details_json,created_at) VALUES (?,?,?,?)',
                  (case_id, kind, json.dumps(details, sort_keys=True), now()))

    @staticmethod
    def add_message(c, case_id, role, content):
        message_id = str(uuid4())
        c.execute('INSERT INTO case_messages VALUES (?,?,?,?,?)', (message_id, case_id, role, content, now()))
        return message_id

    def create_case(self, customer_id, message, *, secrets=()):
        self.catalog.customer(customer_id)
        message = validate_message(message, secrets)
        case_id, request_id = str(uuid4()), str(uuid4())
        with self.locked() as c:
            c.execute('INSERT INTO refund_requests(id,message,created_at) VALUES (?,?,?)', (request_id, message, now()))
            c.execute("INSERT INTO refund_cases(id,customer_id,request_id,stage,created_at,updated_at) VALUES (?,?,?,'collecting_details',?,?)",
                      (case_id, customer_id, request_id, now(), now()))
            self.add_message(c, case_id, 'customer', message)
            self.event(c, case_id, 'case_opened', {})
        return self.snapshot(customer_id, case_id)

    def list_cases(self, customer_id):
        self.catalog.customer(customer_id)
        with self.connect() as c:
            return [dict(r) for r in c.execute('''SELECT f.id,f.stage,f.updated_at,f.amount_minor,
                COALESCE(o.description,r.message) AS title FROM refund_cases f
                JOIN refund_requests r ON r.id=f.request_id
                LEFT JOIN customer_orders o ON o.customer_id=f.customer_id AND o.order_id=f.selected_order
                WHERE f.customer_id=? ORDER BY f.updated_at DESC''', (customer_id,))]

    def related_cases(self, customer_id, case_id):
        """Scoped notice only: separate confirmations remain separate operations."""
        with self.connect() as c:
            case = self.owned(c, customer_id, case_id)
            return [dict(r) for r in c.execute('''SELECT f.id,f.stage,p.amount_minor
                FROM refund_cases f JOIN case_proposals p ON p.case_id=f.id AND p.revision=f.revision
                JOIN case_proposals current ON current.case_id=? AND current.revision=?
                WHERE f.customer_id=? AND f.id<>? AND p.payment_id=current.payment_id
                  AND (f.operation_id IS NOT NULL OR f.stage='awaiting_confirmation')
                ORDER BY f.updated_at DESC''', (case_id, case['revision'], customer_id, case_id))]

    def snapshot(self, customer_id, case_id):
        with self.connect() as c:
            c.execute('BEGIN')
            case = self.owned(c, customer_id, case_id)
            case['candidates'] = json.loads(case.pop('candidates_json'))
            case['messages'] = [dict(r) for r in c.execute('SELECT * FROM case_messages WHERE case_id=? ORDER BY rowid', (case_id,))]
            for message in case['messages']:
                if message['role'] == 'customer_form':
                    message['fields'] = json.loads(message['content'])
            case['events'] = [{**dict(r), 'details':json.loads(r['details_json'])} for r in c.execute('SELECT * FROM case_events WHERE case_id=? ORDER BY id', (case_id,))]
            case['proposals'] = [dict(r) for r in c.execute('SELECT * FROM case_proposals WHERE case_id=? ORDER BY revision', (case_id,))]
            case['agent_turns'] = [dict(r) for r in c.execute('SELECT * FROM agent_turns WHERE case_id=? ORDER BY rowid', (case_id,))]
            case['remembered_details'] = read_memory(c, case_id)['facts']
        return case

    def message(self, customer_id, case_id, message, version, *, secrets=(), status_followup=False):
        message = validate_message(message, secrets)
        with self.locked() as c:
            case = self.owned(c, customer_id, case_id)
            if status_followup and case['operation_id']:
                if case['version'] != version:
                    reject('stale_case', 'This case changed. Reload it before continuing.')
            else:
                self.editable(case, version)
            mid = self.add_message(c, case_id, 'customer', message)
            if not case['operation_id']:
                removed = invalidate(c, case_id, mid, message)
                if removed:
                    self.event(c, case_id, 'case_memory_invalidated', {'fields': removed, 'message_id': mid})
            c.execute("UPDATE refund_cases SET stage=?,question=NULL,version=version+1,updated_at=? WHERE id=?",
                      (case['stage'] if case['operation_id'] else 'collecting_details', now(), case_id))
            self.event(c, case_id, 'customer_message', {'message_id':mid, 'previous_proposal_invalidated':(case['revision'] or None) if not case['operation_id'] else None})
        return self.snapshot(customer_id, case_id)

    def clarify(self, customer_id, case_id, candidates, question, version):
        question = validate_message(question)
        if not isinstance(candidates, list) or len(candidates) > 10:
            reject('invalid_candidates', 'Use at most ten owned order IDs.')
        for reference in candidates:
            self.catalog.order(customer_id, reference)
        with self.locked() as c:
            case = self.owned(c, customer_id, case_id)
            self.editable(case, version)
            c.execute("UPDATE refund_cases SET candidates_json=?,question=?,stage='awaiting_clarification',version=version+1,updated_at=? WHERE id=?",
                      (json.dumps(candidates), question, now(), case_id))
            self.add_message(c, case_id, 'agent', question)
            self.event(c, case_id, 'clarification_requested', {'candidates': candidates, 'question':question})
        return self.snapshot(customer_id, case_id)

    def cancel(self, customer_id, case_id, version):
        with self.locked() as c:
            case = self.owned(c, customer_id, case_id)
            self.editable(case, version)
            c.execute("UPDATE refund_cases SET stage='cancelled',version=version+1,updated_at=? WHERE id=?", (now(), case_id))
            self.event(c, case_id, 'case_cancelled', {'revision': case['revision']})
        return self.snapshot(customer_id, case_id)

    def claim(self, customer_id, case_id, revision, version):
        with self.locked() as c:
            case = self.owned(c, customer_id, case_id)
            if type(revision) is not int or revision != case['revision'] or revision < 1:
                reject('stale_proposal', 'Confirm the current proposal after reviewing it again.')
            if case['operation_id']:
                return case['operation_id']  # Same confirmed revision: durable replay.
            self.editable(case, version)
            if case['stage'] != 'awaiting_confirmation':
                reject('confirmation_not_ready', 'This case has no current proposal to confirm.')
            proposal = c.execute('SELECT * FROM case_proposals WHERE case_id=? AND revision=?', (case_id, revision)).fetchone()
            if not c.execute('SELECT 1 FROM customer_orders WHERE customer_id=? AND payment_id=? AND order_id=?',
                             (customer_id, proposal['payment_id'], proposal['order_id'])).fetchone():
                reject('order_not_found', 'The purchase no longer belongs to this demo customer.')
            approval = self.approve_in_transaction(c, proposal['analysis_id'], proposal['payment_id'], proposal['amount_minor'], proposal['currency'])
            c.execute("UPDATE refund_cases SET stage='processing',confirmed_revision=?,operation_id=?,version=version+1,updated_at=? WHERE id=?",
                      (revision, approval['operation_id'], now(), case_id))
            self.event(c, case_id, 'confirmation_received', {'revision':revision, 'operation_id':approval['operation_id']})
        return approval['operation_id']


class CaseService:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    def propose(self, customer_id, case_id, order_id, amount_minor, currency, version, *, grounding=None):
        case = self.store.snapshot(customer_id, case_id)
        self.store.editable(case, version)
        full = amount_minor is None and grounding and grounding.get('refund_type') == 'full'
        if (not full and (type(amount_minor) is not int or amount_minor <= 0)) or currency != 'usd':
            reject('invalid_refund_amount', 'Specify a positive integer USD-cent amount.')
        inspected = self.store.catalog.inspect_payment(customer_id, order_id, self.gateway)
        order = inspected['order']
        if full:
            amount_minor = inspected['payment']['refundable_amount']
        decision = DryRunPolicy(self.store, lambda _: inspected['payment']).assess_payment(order['payment_id'], amount_minor, currency)
        if decision['action'] != 'propose_refund':
            reject(decision['code'], 'The purchase is not eligible for this refund proposal.')
        with self.store.locked() as c:
            current = self.store.owned(c, customer_id, case_id)
            self.store.editable(current, version)  # A correction/cancel may have won during GET.
            revision = current['revision'] + 1
            if grounding:
                message_id = grounding['amount_message_id']
                if not c.execute("SELECT 1 FROM case_messages WHERE id=? AND case_id=? AND role='customer'", (message_id, case_id)).fetchone():
                    reject('invalid_evidence', 'The proposal must cite a customer message in this case.')
                # A direct proposal may skip a grounded inspection. Preserve its
                # validated purchase selection when later edits revoke the card.
                selection = AgentAction(action='inspect_payment', order_id=order['order_id'],
                                        order_message_id=grounding['order_message_id'],
                                        order_evidence=grounding['order_evidence'])
                changed = remember(c, case, selection, self.store.catalog, customer_id)
                if changed:
                    self.store.event(c, case_id, 'case_memory_saved', {'details': changed})
            else:
                message_id = self.store.add_message(c, case_id, 'customer_form', json.dumps(
                    {'source':'structured_form', 'order_id':order['order_id'], 'amount_minor':amount_minor, 'currency':currency}))
            run = self.store.new_analysis(current['request_id'], 'case-policy-v1', source='agent_grounded_proposal' if grounding else 'structured_customer_proposal', decision=decision, grounding=grounding)
            run['status'] = 'completed'
            self.store.insert_analysis(c, run)
            c.execute('INSERT INTO case_proposals VALUES (?,?,?,?,?,?,?,?,?)',
                      (case_id, revision, run['id'], order['order_id'], order['payment_id'], amount_minor, currency, message_id, now()))
            c.execute("UPDATE refund_cases SET stage='awaiting_confirmation',revision=?,selected_order=?,amount_minor=?,currency=?,question=NULL,version=version+1,updated_at=? WHERE id=?",
                      (revision, order['order_id'], amount_minor, currency, now(), case_id))
            self.store.event(c, case_id, 'proposal_validated', {'revision':revision, 'order_id':order['order_id'], 'payment_id':order['payment_id'], 'amount_minor':amount_minor,
                                                              'currency':currency, 'remaining_minor':decision['remaining_minor'], 'observed_at':decision['observed_at'], 'grounding':grounding})
        return self.store.snapshot(customer_id, case_id)

    def confirm(self, customer_id, case_id, revision, version):
        self.store.claim(customer_id, case_id, revision, version)
        return self.resume(customer_id, case_id)

    def resume(self, customer_id, case_id, *, read_only=False):
        case = self.store.snapshot(customer_id, case_id)
        if not case['operation_id']:
            # Refresh mutable state but never confirm or submit on resume.
            if case['selected_order']:
                self.store.catalog.inspect_payment(customer_id, case['selected_order'], self.gateway)
            return case
        proposal = next(p for p in case['proposals'] if p['revision'] == case['confirmed_revision'])
        with self.store.locked() as c:
            operation = self.store.operation(c, case['operation_id'])
            if read_only and (not operation or not operation['stripe_id']):
                return case  # A model status tool can never replay a POST.
            self.store.event(c, case_id, 'operation_resume_started', {'operation_id':case['operation_id'], 'read_only':bool(operation and operation['stripe_id'])})
        error = None
        try:
            ReviewedExecution(self.store, self.gateway, case_customer_id=customer_id).execute(
                proposal['analysis_id'], verify_only=bool(operation and operation['stripe_id']))
        except SandboxError as caught:
            error = caught.code  # Fixed codes only, no upstream payloads.
        with self.store.locked() as c:
            operation = self.store.operation(c, case['operation_id'])
            # Stored provider state, not an LLM's claimed success, drives the case.
            stage = operation['status'] if operation else 'processing'
            if error or stage == 'unresolved':
                stage = 'needs_reconciliation'
            c.execute('UPDATE refund_cases SET stage=?,refund_id=?,last_error=?,observed_at=?,version=version+1,updated_at=? WHERE id=?',
                      (stage, operation['stripe_id'] if operation else None, error, now() if not error else case['observed_at'], now(), case_id))
            self.store.event(c, case_id, 'operation_observed', {'operation_id':case['operation_id'], 'stage':stage,
                                                            'refund_id':operation['stripe_id'] if operation else None, 'error':error})
            if stage != case['stage'] or (operation and operation['stripe_id'] != case['refund_id']):
                if stage == 'verified':
                    message = f"Your refund is verified by Stripe. Refund ID: {operation['stripe_id']}."
                elif stage == 'pending':
                    message = 'Stripe is still processing your refund. I will check for an update automatically while this app is running.'
                elif error == 'invalid_refund_amount':
                    message = ('The confirmed amount no longer fits the available balance. No smaller amount was '
                               'substituted. This case needs reconciliation; the existing operation is preserved.')
                else:
                    message = 'The refund is not verified yet. Your existing operation is saved; I will recover temporary interruptions automatically within the retry limits.'
                self.store.add_message(c, case_id, 'agent', message)
        return self.store.snapshot(customer_id, case_id)
