"""Bounded model action/observation loop. It has no refund execution tool."""

import json
import re
import time
from datetime import date
from uuid import uuid4

from pydantic import ValidationError

from .agent_contract import AgentAction, PROMPT_VERSION, SYSTEM, ground, ground_order, proposal_feedback, request_is_paused
from .cases import CaseService, now
from .catalog import last_week
from .gemini_gateway import GeminiExtractor, ExtractionError
from .stripe_gateway import SandboxError
from .workflow import reject
from .case_memory import remember


class GeminiPlanner(GeminiExtractor):
    response_schema = AgentAction
    system_instruction = SYSTEM
    prompt_version = PROMPT_VERSION
    result_key = 'action'
    request_retries = 0  # One HTTP attempt per step; retries consume a new turn.

    def step(self, context, *, remaining_seconds):
        payload = json.dumps(context, sort_keys=True)
        if len(payload) > 60000:
            raise ExtractionError('context_limit', 'This case exceeds the current context limit.')
        if any(secret and secret in payload for secret in self._secrets):
            raise ExtractionError('credential_in_context', 'Remove credentials from this case.')
        self.request_timeout_ms = max(1, min(20000, int(remaining_seconds * 1000)))
        self.request_deadline = time.monotonic() + max(.001, remaining_seconds)
        return self._extract_validated(payload)


def build_context(store, customer_id, case, mode, observations, remaining):
    messages = [{'id':m['id'], 'role':m['role'], 'content':m['content']} for m in case['messages']]
    past = [{'kind':e['kind'], 'details':e['details']} for e in case['events']
            if e['kind'] in ('agent_tool_result', 'proposal_validated', 'clarification_requested')]
    context = {'scenario_date':store.catalog.customer(customer_id)['scenario_date'],
               'execution': {'confirmed':bool(case['operation_id']), 'stage':case['stage'], 'refund_id':case['refund_id']},
               'remaining_tool_calls':remaining, 'this_turn_observations':observations,
               'context_version':'case-context-v2', 'mode':mode}
    if mode == 'history':
        context.update(messages=messages, observations=past)
    elif mode == 'structured':
        grounding = next((e['details'].get('grounding') for e in reversed(case['events'])
                          if e['kind'] == 'proposal_validated' and e['details'].get('grounding')), {})
        cited = {v for k,v in grounding.items() if k.endswith('_message_id')}
        partial = case.get('remembered_details', {})
        cited.update(fact['message_id'] for fact in partial.values())
        # Preserve customer evidence even when omitted from the recent window.
        recent_ids = {m['id'] for m in messages[-8:]}
        context.update(messages=[m for m in messages if m['id'] in recent_ids | cited],
                       memory={'candidates':[store.catalog.order(customer_id, r) for r in case['candidates']],
                               'selected_order':case['selected_order'], 'amount_minor':case['amount_minor'],
                               'currency':case['currency'], 'question':case['question'],
                               'proposal_revision':case['revision'], 'grounding':grounding,
                               'partial_details': partial},
                       observations=past[-4:])
    else:
        raise ValueError('Unknown agent context mode')
    if len(json.dumps(context)) > 60000:
        raise ExtractionError('context_limit', 'This case exceeds the current context limit; start a separate case for a new request.')
    return context


class AgentRunner:
    def __init__(self, store, gateway, planner, *, tool_budget=5, deadline_seconds=120, context_mode='structured'):
        if type(tool_budget) is not int or not 1 <= tool_budget <= 10 or not 1 <= deadline_seconds <= 180 or context_mode not in ('history','structured'):
            raise ValueError('Invalid agent limits or context mode')
        self.store, self.gateway, self.planner = store, gateway, planner
        self.tool_budget, self.deadline_seconds, self.context_mode = tool_budget, deadline_seconds, context_mode

    def _check(self, c, customer, case_id, turn_id):
        case = self.store.owned(c, customer, case_id)
        turn = c.execute('SELECT * FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
        if turn['status'] != 'running' or case['version'] != turn['case_version']:
            reject('agent_superseded', 'A newer case action replaced this turn.')
        if time.time() >= turn['expires_at']:
            reject('agent_time_limit', 'The turn reached its time budget.')
        return case

    def _record(self, customer, case_id, turn_id, kind, details):
        with self.store.locked() as c:
            self._check(c, customer, case_id, turn_id)
            self.store.event(c, case_id, kind, {'turn_id':turn_id, **details})

    def _model_attempts(self, customer, case_id, turn_id, records):
        for index, record in enumerate(records):
            if index:
                self._record(customer, case_id, turn_id, 'agent_model_fallback',
                             {'from_model': records[index - 1]['model'], 'to_model': record['model'],
                              'reason': records[index - 1]['status']})
            self._record(customer, case_id, turn_id, 'agent_model_attempt', record)

    def _sync(self, customer, case_id, turn_id, result):
        with self.store.locked() as c:
            current = self.store.owned(c, customer, case_id)
            turn = c.execute('SELECT * FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
            if turn['status'] != 'running' or current['version'] != result['version']:
                reject('agent_superseded', 'A newer case action replaced this turn.')
            c.execute('UPDATE agent_turns SET case_version=? WHERE id=?', (result['version'],turn_id))

    def _close(self, customer, case_id, turn_id, status, message):
        with self.store.locked() as c:
            case = self.store.owned(c, customer, case_id)
            turn = c.execute('SELECT * FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
            if turn['status'] != 'running':
                return
            if case['version'] != turn['case_version']:
                status, message = 'superseded', None
            if message:
                self.store.add_message(c, case_id, 'agent', message)
                c.execute('UPDATE refund_cases SET version=version+1,updated_at=? WHERE id=?', (now(),case_id))
            c.execute('UPDATE agent_turns SET status=? WHERE id=?', (status,turn_id))
            self.store.event(c, case_id, 'agent_turn_finished', {'turn_id':turn_id, 'status':status})

    def _clarify_and_close(self, customer, case_id, turn_id, question):
        current = self.store.snapshot(customer, case_id)
        with self.store.locked() as c:
            self._check(c, customer, case_id, turn_id)
        result = self.store.clarify(customer, case_id, current['candidates'], question, current['version'])
        self._sync(customer, case_id, turn_id, result)
        self._close(customer, case_id, turn_id, 'completed', None)

    @staticmethod
    def _call_key(action):
        fields = {
            'search_orders': ('query', 'last_week'),
            'inspect_payment': ('order_id', 'order_message_id', 'order_evidence'),
            'get_refund_status': (),
            'propose_refund': ('order_id', 'order_message_id', 'order_evidence', 'refund_type',
                              'amount', 'amount_message_id', 'amount_evidence',
                              'currency_message_id', 'currency_evidence'),
        }
        values = action.model_dump()
        return (action.action, *(values[name] for name in fields[action.action]))

    def _repeat_question(self, customer, case, previous):
        error = previous.get('error')
        if error in ('ambiguous_order_evidence', 'conflicting_order_evidence', 'invalid_evidence'):
            reason = 'purchase'
        elif error == 'missing_currency_evidence':
            reason = 'currency'
        elif error in ('invalid_amount_evidence', 'stale_amount_evidence', 'invalid_full_evidence'):
            reason = 'amount'
        else:
            facts = case.get('remembered_details', {})
            reason = ('purchase' if 'order' not in facts else 'amount' if 'amount' not in facts
                      else 'currency' if facts['amount']['refund_type'] == 'partial' and 'currency' not in facts
                      else 'unsupported')
        return self.question(reason, customer, case)

    def run(self, customer, case_id, *, expected_version=None):
        if hasattr(self.planner, 'begin_turn'):
            self.planner.begin_turn()
        turn_id = str(uuid4())
        with self.store.locked() as c:
            case = self.store.owned(c, customer, case_id)
            if expected_version is not None and case['version'] != expected_version:
                reject('agent_superseded', 'A newer customer message replaced this queued turn.')
            if case['stage'] in ('cancelled', 'awaiting_confirmation'):
                reject('agent_turn_not_needed', 'Review the current proposal or send a correction first.')
            active = c.execute("SELECT * FROM agent_turns WHERE case_id=? AND status='running'", (case_id,)).fetchone()
            if active:
                if active['expires_at'] > time.time() and active['case_version'] == case['version']:
                    reject('agent_turn_running', 'The agent is already working on this turn. Reload shortly.')
                c.execute("UPDATE agent_turns SET status='interrupted' WHERE id=?", (active['id'],))
            c.execute('UPDATE refund_cases SET version=version+1 WHERE id=?', (case_id,))
            c.execute("INSERT INTO agent_turns VALUES (?,?,?,'running',?,?,?,0,0,?)",
                      (turn_id, case_id, case['version']+1, self.context_mode, self.planner.model,
                       time.time()+self.deadline_seconds, now()))
            self.store.event(c, case_id, 'agent_turn_started', {'turn_id':turn_id, 'model':self.planner.model,
                            'context_mode':self.context_mode, 'prompt_version':PROMPT_VERSION, 'tool_budget':self.tool_budget})
        deadline, calls, observations = time.monotonic()+self.deadline_seconds, 0, []
        seen_calls = {}
        try:
            current = self.store.snapshot(customer, case_id)
            if not current['operation_id'] and request_is_paused(current):
                self._record(customer, case_id, turn_id, 'agent_waiting_for_customer', {'reason': 'paused_request'})
                question = (
                    'No new refund will be submitted. When you are ready, tell me the amount with currency '
                    'or ask for the full remaining refund. If your request is conditional, clarify whether to proceed.')
                latest = next(m['content'] for m in reversed(current['messages']) if m['role'] == 'customer')
                if re.search(r"(?:don't|do not) refund (?:the )?(?:full|entire)", latest, re.I):
                    question = ('Do you want a partial refund instead? Please restate just the requested amount '
                                'with currency and purchase, for example: Refund USD 5 from [your order ID]. '
                                'No refund has been submitted.')
                self._clarify_and_close(customer, case_id, turn_id, question)
                return self.store.snapshot(customer, case_id)
            for step in range(self.tool_budget + 2):
                case = self.store.snapshot(customer, case_id)
                context = build_context(self.store, customer, case, self.context_mode, observations, self.tool_budget-calls)
                self._record(customer, case_id, turn_id, 'agent_model_started', {'step':step+1})
                with self.store.locked() as c:
                    self._check(c, customer, case_id, turn_id)
                    c.execute('UPDATE agent_turns SET model_steps=model_steps+1 WHERE id=?', (turn_id,))
                response = self.planner.step(context, remaining_seconds=max(.001,deadline-time.monotonic()))
                self._model_attempts(customer, case_id, turn_id, response.get('model_attempts', []))
                action = AgentAction.model_validate(response['action'])
                payload = action.model_dump()
                self._record(customer, case_id, turn_id, 'agent_model_result',
                             {'step':step+1,'action':payload,'usage':response.get('usage'), 'attempts':response.get('attempts'),
                              'latency_ms':response.get('latency_ms'), 'model':response.get('model', self.planner.model),
                              'pacing_wait_ms':response.get('pacing_wait_ms', 0), 'usage_complete':response.get('usage_complete', True)})
                with self.store.locked() as c:
                    self._check(c, customer, case_id, turn_id)
                if action.action in ('ask','finish'):
                    if case['operation_id']:
                        if not any(o.get('tool') == 'get_refund_status' and 'error' not in o for o in observations):
                            action = AgentAction(action='get_refund_status')
                        else:
                            result = self.store.snapshot(customer, case_id)
                            self._close(customer, case_id, turn_id, 'completed', self.status_message(result))
                            break
                    else:
                        with self.store.locked() as c:
                            self._check(c, customer, case_id, turn_id)
                            try:
                                changed = remember(c, case, action, self.store.catalog, customer)
                            except SandboxError as error:
                                # Optional memory claims validate atomically. Rejection
                                # must not prevent the customer receiving a clarification.
                                changed = {}
                                self.store.event(c, case_id, 'case_memory_rejected',
                                                 {'error': error.code, 'turn_id': turn_id})
                            if changed:
                                self.store.event(c, case_id, 'case_memory_saved', {'details': changed})
                                c.execute('UPDATE refund_cases SET version=version+1 WHERE id=?', (case_id,))
                                c.execute('UPDATE agent_turns SET case_version=case_version+1 WHERE id=?', (turn_id,))
                        case = self.store.snapshot(customer, case_id)
                        question = self.question(action.reason, customer, case)
                        result = self.store.clarify(customer, case_id, case['candidates'], question, case['version'])
                        self._sync(customer, case_id, turn_id, result)
                        self._close(customer, case_id, turn_id, 'completed', None)
                        break
                call_key = self._call_key(action)
                if call_key in seen_calls:
                    self._record(customer, case_id, turn_id, 'agent_loop_stopped',
                                 {'tool': action.action, 'reason': 'repeated_call',
                                  'previous_error': seen_calls[call_key].get('error')})
                    self._clarify_and_close(customer, case_id, turn_id,
                                           self._repeat_question(customer, case, seen_calls[call_key]))
                    break
                if calls >= self.tool_budget:
                    self._close(customer, case_id, turn_id, 'budget_exhausted', 'I reached this turn’s tool limit. Your case is saved. Please clarify the purchase/amount or retry the agent.')
                    break
                calls += 1
                self._record(customer, case_id, turn_id, 'agent_tool_started', {'tool':action.action,'arguments':action.model_dump()})
                with self.store.locked() as c:
                    self._check(c, customer, case_id, turn_id)
                    c.execute('UPDATE agent_turns SET tool_calls=tool_calls+1 WHERE id=?', (turn_id,))
                try:
                    observation = self.dispatch(customer, case, turn_id, action)
                except SandboxError as error:
                    if error.code in ('stale_case','case_locked','agent_superseded','agent_time_limit'):
                        raise
                    observation = {'error':error.code}
                    if error.code == 'stale_memory_evidence':
                        observation.update(rejected_arguments=action.model_dump(exclude_none=True),
                            repair='A source message predates a customer correction or restatement. '
                                   'Use the latest customer message that explicitly supplies each detail, '
                                   'even when the order/value is unchanged. Replace the old source message ID '
                                   'and quote; if the latest message supplies it, do not ask the customer again.')
                    if action.action == 'propose_refund' and error.code in (
                            'invalid_evidence', 'invalid_amount_evidence', 'invalid_full_evidence',
                            'missing_currency_evidence', 'ambiguous_order_evidence',
                            'conflicting_order_evidence', 'stale_amount_evidence'):
                        observation.update(proposal_feedback(action))
                observation = {'tool':action.action, **observation}
                observations.append(observation)
                seen_calls[call_key] = observation
                self._record(customer, case_id, turn_id, 'agent_tool_result', observation)
                if observation.get('error') == 'order_not_found':
                    current = self.store.snapshot(customer, case_id)
                    self._clarify_and_close(customer, case_id, turn_id,
                        "I couldn't find that purchase in this demo customer's orders. "
                        + self.question('purchase', customer, current))
                    break
                if action.action == 'get_refund_status' and 'error' not in observation:
                    current = self.store.snapshot(customer, case_id)
                    self._close(customer, case_id, turn_id, 'completed', self.status_message(current))
                    break
                if observation.get('selected_payment_fully_refunded'):
                    order_id = observation['order']['order_id']
                    question = (f'{order_id} is already fully refunded according to the payment state I just retrieved. '
                                'There is no remaining balance to refund. No new refund was submitted. '
                                'If you meant a different purchase, tell me its order ID or description.')
                    current = self.store.snapshot(customer, case_id)
                    result = self.store.clarify(customer, case_id, current['candidates'], question, current['version'])
                    self._sync(customer, case_id, turn_id, result)
                    self._close(customer, case_id, turn_id, 'completed', None)
                    break
                if action.action == 'propose_refund' and 'error' not in observation:
                    self._close(customer, case_id, turn_id, 'completed', 'Please review the refund card. Nothing will be submitted until you confirm the exact purchase and amount.')
                    break
            else:
                self._close(customer, case_id, turn_id, 'budget_exhausted', 'This turn reached its step limit. Your case is saved; please retry or clarify the request.')
        except (ExtractionError, SandboxError, ValidationError, KeyError) as error:
            code = getattr(error, 'code', 'invalid_agent_output')
            if isinstance(error, ExtractionError):
                try:
                    self._model_attempts(customer, case_id, turn_id, getattr(error, 'model_attempts', []))
                except SandboxError:
                    pass  # Expired/superseded turns cannot append new actions.
            self._close(customer, case_id, turn_id, code,
                        'I could not finish this turn. Your case is saved and no new refund was authorized. Temporary interruptions can be retried automatically within the retry limits; see the case status for the next action.')
        return self.store.snapshot(customer, case_id)

    def dispatch(self, customer, case, turn_id, action):
        case_id = case['id']
        if case['operation_id'] and action.action != 'get_refund_status':
            reject('case_locked', 'A confirmed case only permits status retrieval.')
        if action.action == 'search_orders':
            start, end = last_week() if action.last_week else (None,None)
            orders = self.store.catalog.search(customer, action.query or '', purchased_from=start, purchased_to=end)[:10]
            with self.store.locked() as c:
                self._check(c, customer, case_id, turn_id)
                c.execute('UPDATE refund_cases SET candidates_json=?,version=version+1 WHERE id=?', (json.dumps([o['order_id'] for o in orders]),case_id))
                c.execute('UPDATE agent_turns SET case_version=case_version+1 WHERE id=?',(turn_id,))
            return {'orders':orders}
        if action.action == 'inspect_payment':
            remembered = case.get('remembered_details', {}).get('order')
            selection = None
            if remembered:
                candidate = AgentAction(action='inspect_payment', order_id=remembered['order_id'],
                    order_message_id=remembered['message_id'], order_evidence=remembered['evidence'])
                # Revalidate against current messages, including corrections.
                try:
                    ground_order(candidate, case, self.store.catalog, customer)
                    selection = candidate
                except SandboxError:
                    pass
            if selection and selection.order_id != action.order_id:
                reject('conflicting_order_evidence', 'Inspect the selected purchase unless the customer changes it.')
            if action.order_message_id and action.order_evidence:
                candidate = AgentAction(action='inspect_payment', order_id=action.order_id,
                    order_message_id=action.order_message_id, order_evidence=action.order_evidence)
                ground_order(candidate, case, self.store.catalog, customer)
                selection = candidate
            result = self.store.catalog.inspect_payment(customer, action.order_id, self.gateway)
            if selection:
                with self.store.locked() as c:
                    self._check(c, customer, case_id, turn_id)
                    changed = remember(c, case, selection, self.store.catalog, customer)
                    if changed:
                        self.store.event(c, case_id, 'case_memory_saved', {'details': changed})
                        c.execute('UPDATE refund_cases SET version=version+1 WHERE id=?', (case_id,))
                        c.execute('UPDATE agent_turns SET case_version=case_version+1 WHERE id=?', (turn_id,))
                payment = result['payment']
                if (payment.get('paid') and payment.get('captured') and
                        payment.get('amount_captured', 0) > 0 and
                        payment.get('amount_refunded', 0) >= payment['amount_captured'] and
                        payment.get('refundable_amount') == 0):
                    result['selected_payment_fully_refunded'] = True
            return result
        if action.action == 'propose_refund':
            order, amount, evidence = ground(action, case, self.store.catalog, customer)
            result = CaseService(self.store, self.gateway).propose(customer, case_id, order['order_id'], amount, 'usd', case['version'], grounding=evidence)
            self._sync(customer, case_id, turn_id, result)
            return {'proposal':result['proposals'][-1], 'stage':result['stage']}
        if action.action == 'get_refund_status':
            result = CaseService(self.store, self.gateway).resume(customer, case_id, read_only=True)
            self._sync(customer, case_id, turn_id, result)
            return {'stage':result['stage'], 'refund_id':result['refund_id'], 'last_error':result['last_error'], 'observed_at':result['observed_at']}
        reject('unsupported_tool', 'Choose an available tool.')

    def question(self, reason, customer, case):
        scope = 'This demo handles one purchase per case. '
        if reason == 'purchase' and case['candidates']:
            options = [self.store.catalog.order(customer, ref) for ref in case['candidates']]
            return scope + 'Which purchase did you mean? ' + '; '.join(f"{o['order_id']} on {date.fromisoformat(o['purchased_on']).strftime('%A')}, {o['purchased_on']}, for USD {o['amount_minor']/100:.2f}" for o in options) + '.'
        return {'amount':'How much would you like refunded? State the amount with currency, or ask for the full remaining refund.',
                'currency':'What currency is that refund amount in? This demo supports USD.',
                'unsupported':'Please restate the refund request clearly, with one purchase and a numeric USD amount or an explicit full refund.'}.get(reason, scope + 'Which purchase should I look for? Give its description, purchase date, or order ID.')

    @staticmethod
    def status_message(case):
        if not case['operation_id']:
            return 'No refund has been authorized through this case. Review and confirm a refund proposal before submission.'
        latest = next((m['content'] for m in reversed(case['messages']) if m['role'] == 'customer'), '')
        prefix = ("This refund was already authorized and may have been submitted. I can't cancel it here; "
                  'I can check its existing operation. ') if re.search(r"\b(?:cancel|stop)\b|don't refund|do not refund", latest, re.I) else ''
        if case['stage'] == 'verified':
            return prefix + f"Stripe verification confirmed your refund: {case['refund_id']}."
        if case.get('last_error') == 'invalid_refund_amount':
            prefix += 'The confirmed amount no longer fits the available balance. It has not been silently reduced. '
        return prefix + f"The latest case state is {case['stage'].replace('_',' ')}. " + (f"Refund ID: {case['refund_id']}." if case['refund_id'] else 'No verified refund ID is available. An existing confirmed operation is preserved for recovery; see the case status for automatic progress or a paused recovery action.')
