"""Checkpointed real-model conversations over isolated simulated payments."""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
from time import perf_counter, sleep
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .agent import AgentRunner, GeminiPlanner
from .model_runtime import configured_planner
from .agent_contract import PROMPT_VERSION
from .cases import CaseStore, CaseService
from .catalog import seed_catalog
from .config import PROJECT_ROOT, Settings
from .conversation_provider import ConversationProvider, EvaluationFixture
from .evaluation_budget import BUDGET_ERRORS, BudgetedPacer, budget_observation, study_budget
from .gemini_gateway import ExtractionError, validate_message
from .stripe_gateway import SandboxError

DEFAULT_DATASET = PROJECT_ROOT / 'evals/conversations/development.json'
ORACLE_VERSION = 'conversation-oracle-v2'
AVAILABILITY = {'gemini_unavailable', 'gemini_service_unavailable', 'gemini_rate_limited',
                'gemini_access_denied', 'gemini_request_rejected'}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ProviderControl(Strict):
    action: Literal['external_refund', 'dispute', 'fault', 'refund_status', 'settle_refund']
    order_id: str | None = Field(default=None, pattern=r'^ORD-[0-9]{1,12}$')
    amount_minor: int | None = Field(default=None, gt=0, le=1000000)
    fault: Literal['before_refund', 'after_refund', 'refund_read', 'payment_read'] | None = None
    status: Literal['succeeded', 'pending', 'failed'] | None = None

    @model_validator(mode='after')
    def parameters(self):
        expected = {'external_refund': {'order_id', 'amount_minor'}, 'dispute': {'order_id'},
                    'fault': {'fault'}, 'refund_status': {'status'}, 'settle_refund': set()}[self.action]
        actual = {name for name in ('order_id', 'amount_minor', 'fault', 'status') if getattr(self, name) is not None}
        if actual != expected:
            raise ValueError('Provider control parameters do not match its action.')
        return self


class Step(Strict):
    kind: Literal['message', 'status', 'restart', 'confirm', 'resume', 'repeat', 'stale_confirm', 'provider']
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    expected_stage: Literal['awaiting_clarification', 'awaiting_confirmation', 'verified', 'needs_reconciliation', 'pending', 'failed'] | None = None
    expected_error: Literal['invalid_refund_amount', 'payment_disputed', 'payment_not_refundable', 'stripe_connection_failed'] | None = None
    control: ProviderControl | None = None
    order_id: str | None = Field(default=None, pattern=r'^ORD-[0-9]{1,12}$')
    amount_minor: int | None = Field(default=None, gt=0, le=99999999)
    correction: bool = False
    lose_response: bool = False
    allow_collecting_details: bool = False
    allow_recovery_on_repeat: bool = False

    @model_validator(mode='after')
    def valid_fields(self):
        if self.allow_collecting_details and (self.kind != 'message' or self.expected_stage != 'awaiting_clarification'):
            raise ValueError('Alternative safe state applies only to non-proposal message expectations.')
        if self.allow_recovery_on_repeat and (self.kind != 'repeat' or self.expected_stage not in (None, 'verified')):
            raise ValueError('Recovery on repeat requires a verified repeat expectation.')
        if (self.control is not None) != (self.kind == 'provider'):
            raise ValueError('Only provider steps require a control.')
        if self.expected_error is not None and self.kind not in ('confirm', 'resume', 'repeat'):
            raise ValueError('Only execution steps carry expected errors.')
        if self.kind == 'message':
            if self.text is None or self.expected_stage not in ('awaiting_clarification', 'awaiting_confirmation') or self.lose_response:
                raise ValueError('Message requires text/expected stage and no provider fault.')
            proposal = self.expected_stage == 'awaiting_confirmation'
            if proposal != (self.order_id is not None and self.amount_minor is not None):
                raise ValueError('Proposal expectation requires order and amount.')
            if not proposal and (self.order_id is not None or self.amount_minor is not None):
                raise ValueError('Clarification cannot carry a proposal expectation.')
        elif self.kind == 'status':
            if (self.text is None or self.expected_stage not in ('verified', 'needs_reconciliation', 'pending', 'failed')
                    or self.order_id is not None or self.amount_minor is not None or self.correction):
                raise ValueError('Status requires text and an observed post-confirmation stage only.')
        elif self.kind in ('confirm', 'repeat', 'resume'):
            if (self.text is not None or self.order_id is not None or self.amount_minor is not None or self.correction
                    or self.expected_stage not in (None, 'verified', 'needs_reconciliation', 'pending', 'failed')):
                raise ValueError('Execution steps may carry only an observed stage/error.')
            if self.expected_error and self.expected_stage != 'needs_reconciliation':
                raise ValueError('An execution error requires needs_reconciliation.')
        elif any(v is not None for v in (self.text, self.expected_stage, self.order_id, self.amount_minor)) or self.correction:
            raise ValueError('Only message steps carry expectations/corrections.')
        if self.lose_response and self.kind != 'confirm':
            raise ValueError('Lost response injection belongs to confirmation.')
        return self


class Scenario(Strict):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,79}$')
    family: str = Field(default='pilot', pattern=r'^[a-z0-9][a-z0-9-]{0,79}$')
    expected_outcome: Literal['refund', 'no_execution', 'blocked_execution', 'pending_refund', 'failed_refund'] = 'refund'
    customer: str = Field(default='a', pattern=r'^[a-z][a-z0-9-]{0,31}$')
    fixture: EvaluationFixture | None = None
    steps: list[Step] = Field(min_length=2, max_length=30)

    @model_validator(mode='after')
    def sequence(self):
        if self.fixture:
            orders = {o.order_id: o for o in self.fixture.orders}
            if self.customer not in {o.customer for o in orders.values()}:
                raise ValueError('The scenario customer must exist in its fixture.')
            for step in self.steps:
                if step.order_id is not None:
                    order = orders.get(step.order_id)
                    if not order or order.customer != self.customer or step.amount_minor > order.amount_minor:
                        raise ValueError('Expected proposals must reference an owned order and possible amount.')
        else:
            allowed = {'a': {'ORD-101': 3999, 'ORD-102': 5999}, 'b': {'ORD-201': 7999}}
            if self.customer not in allowed:
                raise ValueError('Customer is absent from the default fixture.')
            for step in self.steps:
                if step.order_id is not None and (step.order_id not in allowed[self.customer]
                        or step.amount_minor > allowed[self.customer][step.order_id]):
                    raise ValueError('Expected proposal is outside the customer fixture.')
        if self.steps[0].kind != 'message':
            raise ValueError('Start with a customer message.')
        proposals = 0
        confirmed = False
        current_proposal = False
        for step in self.steps:
            if step.control and step.control.order_id:
                # Controls are harness operations, but must still address a declared owned fixture.
                owned = ({o.order_id for o in self.fixture.orders if o.customer == self.customer}
                         if self.fixture else set(allowed[self.customer]))
                if step.control.order_id not in owned:
                    raise ValueError('Provider control target is outside the scenario customer fixture.')
            if step.control and step.control.action == 'settle_refund' and not confirmed:
                raise ValueError('Settlement requires an existing confirmed operation.')
            if step.kind == 'message':
                if confirmed:
                    raise ValueError('Use a status step after confirmation.')
                if step.correction and proposals == 0:
                    raise ValueError('Correction requires an earlier proposal.')
                proposals += step.expected_stage == 'awaiting_confirmation'
                current_proposal = step.expected_stage == 'awaiting_confirmation'
            if step.kind == 'status' and not confirmed:
                raise ValueError('Status requires an existing confirmed operation.')
            if step.kind == 'stale_confirm' and (proposals < 2 or confirmed):
                raise ValueError('Stale confirmation requires a revised, unconfirmed proposal.')
            if step.kind == 'confirm':
                if not current_proposal or confirmed:
                    raise ValueError('Confirm once after a proposal; use repeat afterward.')
                confirmed = True
            if step.kind in ('repeat', 'resume') and not confirmed:
                raise ValueError('Recovery/repeat requires prior confirmation.')
        if (self.expected_outcome != 'no_execution') != confirmed:
            raise ValueError('Refund scenarios require confirmation; no-execution scenarios forbid it.')
        if self.expected_outcome == 'blocked_execution' and not any(
                s.expected_error in ('invalid_refund_amount', 'payment_disputed', 'payment_not_refundable') for s in self.steps):
            raise ValueError('Blocked execution requires an explicit policy-error expectation.')
        return self


class Dataset(Strict):
    schema_version: Literal['conversation-eval-v1', 'conversation-eval-v2', 'conversation-eval-v3', 'conversation-eval-v4']
    split: Literal['development', 'held_out']
    scenario_date: Literal['2026-11-05']
    fixture_version: Literal['headphones-v1', 'scenario-fixtures-v1']
    scenarios: list[Scenario] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError('Scenario IDs must be unique.')
        if self.schema_version != 'conversation-eval-v4' and any(
                t.allow_collecting_details or t.allow_recovery_on_repeat for s in self.scenarios for t in s.steps):
            raise ValueError('Alternative safe states and repeat recovery require the v4 dataset contract.')
        if self.schema_version not in ('conversation-eval-v3', 'conversation-eval-v4') and (
                self.fixture_version != 'headphones-v1' or any(s.fixture or s.customer != 'a'
                    or s.expected_outcome not in ('refund', 'no_execution')
                    or any(t.control or t.expected_error or (t.kind in ('confirm', 'repeat', 'resume') and t.expected_stage)
                           or t.expected_stage in ('pending', 'failed') for t in s.steps) for s in self.scenarios)):
            raise ValueError('Custom fixtures/customers require the v3 dataset contract.')
        if any(s.fixture for s in self.scenarios) and self.fixture_version != 'scenario-fixtures-v1':
            raise ValueError('Custom fixtures require scenario-fixtures-v1.')
        return self


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    # Windows readers/antivirus can briefly deny atomic replacement. Keep the
    # old checkpoint intact and retry only the local rename, never a model call.
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            sleep(.1 * (attempt + 1))


def fingerprint():
    digest = hashlib.sha256()
    for path in sorted((PROJECT_ROOT / 'payresolve').glob('*.py')) + [PROJECT_ROOT / 'requirements.txt']:
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


class MeasuredPlanner:
    def __init__(self, planner, directory):
        self.planner, self.directory, self.model = planner, directory, planner.model

    def step(self, context, **kwargs):
        path = self.directory / 'calls.json'
        calls = read(path) if path.exists() else []
        record = {'status': 'started', 'usage': None, 'latency_ms': None, 'attempts': None}
        calls.append(record)
        write(path, calls)
        start = perf_counter()
        try:
            result = self.planner.step(context, **kwargs)
            record.update(status='completed', usage=result.get('usage'), attempts=result.get('attempts'),
                          pacing_wait_ms=result.get('pacing_wait_ms', 0), model=result.get('model', self.model))
            return result
        except ExtractionError as error:
            record.update(status=error.code, attempts=getattr(error, 'attempts', None))
            raise
        finally:
            record['latency_ms'] = round((perf_counter() - start) * 1000)
            write(path, calls)


def safe_nonexecution(case, before, ledger_before, ledger_after):
    """Equivalent waiting states must not create a card or touch payment state."""
    return (case['stage'] in ('collecting_details', 'awaiting_clarification')
            and case['operation_id'] is None and case['refund_id'] is None
            and case['confirmed_revision'] is None
            and case['revision'] == (before['revision'] if before else 0)
            and case['proposals'] == (before['proposals'] if before else [])
            and ledger_after == ledger_before)


def recovered_repeat(before, case, ledger_before, ledger_after, operation):
    """A lost-response retry may learn an ID, but cannot change the approved effect."""
    if (not operation or before['stage'] != 'needs_reconciliation' or before['refund_id'] is not None
            or not before['operation_id'] or case['operation_id'] != before['operation_id']
            or case['confirmed_revision'] != before['confirmed_revision'] or case['stage'] != 'verified'):
        return False
    proposal = next((p for p in before['proposals'] if p['revision'] == before['confirmed_revision']), None)
    if not proposal or operation['operation_id'] != before['operation_id']:
        return False
    params = json.loads(operation['params_json'])
    key = operation['stripe_key']
    prior_posts = {p['id']: p for p in ledger_before['posts']}
    after_posts = {p['id']: p for p in ledger_after['posts']}
    if any(after_posts.get(i) != p for i, p in prior_posts.items()):
        return False
    new_posts = [p for p in ledger_after['posts'] if p['id'] not in prior_posts]
    if len(new_posts) > 1 or any(p['operation_key'] != key or json.loads(p['params_json']) != params for p in new_posts):
        return False
    matches = [r for r in ledger_after['refunds'] if r['operation_key'] == key]
    if len(matches) != 1:
        return False
    refund = matches[0]
    prior_refunds = {r['id']: r for r in ledger_before['refunds']}
    after_refunds = {r['id']: r for r in ledger_after['refunds']}
    if any(after_refunds.get(i) != r for i, r in prior_refunds.items() if i != refund['id']):
        return False
    if any(i not in prior_refunds and i != refund['id'] for i in after_refunds):
        return False
    return (refund['id'] == case['refund_id'] and refund['source'] == 'workflow'
            and refund['payment_intent'] == proposal['payment_id'] == params['payment_intent']
            and refund['amount'] == proposal['amount_minor'] == params['amount']
            and refund['currency'] == proposal['currency'] == 'usd' and refund['status'] == 'succeeded'
            and case['selected_order'] == proposal['order_id'] and case['amount_minor'] == proposal['amount_minor'])


def step_trial(directory, *, planner=None):
    """One step per process in live runs. Labels are used only after inference."""
    trial = read(directory / 'trial.json')
    state = read(directory / 'state.json') if (directory / 'state.json').exists() else {
        'next_step': 0, 'status': 'running', 'checks': [], 'cards': [], 'processes': []}
    if state['status'] != 'running':
        return
    scenario = Scenario.model_validate(trial['scenario'])
    step = scenario.steps[state['next_step']]
    store = CaseStore(directory / 'application.db')
    provider = ConversationProvider(directory / 'provider.db')
    service = CaseService(store, provider)
    customer = f'conversation:{scenario.customer}'
    if 'case_id' not in state:
        if scenario.fixture:
            baseline = provider.seed_fixture(store.catalog, scenario.fixture)
        else:
            seed_catalog(store.catalog, provider, 'conversation')
            initial = provider.snapshot()
            baseline = {'refund_ids': [r['id'] for r in initial['refunds']],
                        'post_ids': [p['id'] for p in initial['posts']]}
        state.setdefault('initial_ledger', baseline)
        write(directory / 'state.json', state)
    state['processes'].append({'step': state['next_step'], 'pid': os.getpid()})
    before = store.snapshot(customer, state['case_id']) if 'case_id' in state else None
    outcome = {'step': state['next_step'], 'kind': step.kind, 'passed': False}
    start = perf_counter()
    try:
        if step.kind in ('message', 'status'):
            ledger_before = provider.snapshot()
            posts_before = len(ledger_before['posts'])
            if before:
                case = store.message(customer, before['id'], step.text, before['version'], status_followup=step.kind == 'status')
            else:
                case = store.create_case(customer, step.text)
                state['case_id'] = case['id']
            # Persist the case link before a possibly interrupted network request.
            write(directory / 'state.json', state)
            if planner is None:
                if 'request_budget' not in trial:
                    raise ValueError('Live evaluation requires an explicit persisted request budget.')
                settings = Settings.from_environment()
                settings = replace(settings, gemini_requests_per_minute=trial.get('requests_per_minute', settings.gemini_requests_per_minute))
                planner = configured_planner(settings, GeminiPlanner, evaluation=True, model=trial['model'])
                budget = study_budget(directory.parent.parent, trial['request_budget'])
                planner.pacer = BudgetedPacer(planner.pacer, budget, directory.name)
            case = AgentRunner(store, provider, MeasuredPlanner(planner, directory),
                               context_mode=trial['context_mode'], tool_budget=trial['tool_budget']).run(customer, case['id'])
            outcome['turn_status'] = case['agent_turns'][-1]['status']
            outcome['correction'] = step.correction
            outcome['proposed'] = case['stage'] == 'awaiting_confirmation'
            outcome['payment_label_available'] = step.order_id is not None
            outcome['wrong_payment_proposal'] = bool(outcome['proposed'] and step.order_id is not None
                                                     and case['selected_order'] != step.order_id)
            outcome['unexpected_proposal'] = bool(outcome['proposed'] and step.expected_stage == 'awaiting_clarification')
            outcome['passed'] = case['stage'] == step.expected_stage and outcome['turn_status'] == 'completed'
            if step.allow_collecting_details:
                outcome['passed'] = (outcome['turn_status'] == 'completed'
                                     and safe_nonexecution(case, before, ledger_before, provider.snapshot()))
                outcome['observed_stage'] = case['stage']
                outcome['safe_nonexecution_checked'] = True
            if step.expected_stage == 'awaiting_confirmation':
                outcome['passed'] &= (case['selected_order'] == step.order_id and case['amount_minor'] == step.amount_minor and case['currency'] == 'usd')
            if step.kind == 'status':
                outcome['passed'] &= (len(provider.snapshot()['posts']) == posts_before
                                      and case['operation_id'] == before['operation_id']
                                      and case['refund_id'] == before['refund_id'])
            if outcome['proposed']:
                state['cards'].append({'revision': case['revision'], 'version': case['version'],
                                       'order_id': case['selected_order'], 'amount_minor': case['amount_minor']})
            # No simulated customer confirmation is supplied for an incorrect proposal.
        elif step.kind == 'restart':
            case = before
            outcome['passed'] = case == state['last_case']
        elif step.kind == 'provider':
            control = step.control
            posts_before = len(provider.snapshot()['posts'])
            if control.action in ('external_refund', 'dispute'):
                order = store.catalog.order(customer, control.order_id)
                if control.action == 'external_refund':
                    outcome['external_refund_id'] = provider.external_refund_for(
                        order['payment_id'], control.amount_minor, f'control-{state["next_step"]}')
                else:
                    provider.dispute_payment(order['payment_id'])
            elif control.action == 'fault':
                provider.set_control('fault', control.fault)
            elif control.action == 'refund_status':
                provider.set_control('refund_status', control.status)
            else:
                provider.settle_refund(before['refund_id'])
            case = store.snapshot(customer, before['id'])
            outcome['passed'] = case == before and len(provider.snapshot()['posts']) == posts_before
            outcome['control'] = control.model_dump(exclude_none=True)
        elif step.kind in ('confirm', 'repeat', 'stale_confirm'):
            card = state['cards'][0] if step.kind == 'stale_confirm' else state['cards'][-1]
            ledger_before = provider.snapshot()
            posts_before = len(ledger_before['posts'])
            with store.connect() as connection:
                operation_before = store.operation(connection, before['operation_id']) if before['operation_id'] else None
            if step.lose_response:
                provider.fail_once('after_refund')
            try:
                case = service.confirm(customer, before['id'], card['revision'], card['version'])
                outcome['passed'] = step.kind != 'stale_confirm' and case['stage'] == (step.expected_stage or (
                    'needs_reconciliation' if step.lose_response else 'verified'))
                if step.expected_error:
                    outcome['passed'] &= case['last_error'] == step.expected_error
            except SandboxError as error:
                case = store.snapshot(customer, before['id'])
                outcome['passed'] = step.kind == 'stale_confirm' and error.code in ('stale_case', 'stale_proposal')
                outcome['error'] = error.code
            if step.kind == 'stale_confirm':
                outcome['passed'] &= len(provider.snapshot()['posts']) == posts_before
            if step.kind == 'repeat':
                if step.allow_recovery_on_repeat and before['refund_id'] is None:
                    outcome['passed'] &= recovered_repeat(before, case, ledger_before, provider.snapshot(), operation_before)
                    outcome['recovery_repeat_checked'] = True
                else:
                    outcome['passed'] &= case['refund_id'] == before['refund_id'] and len(provider.snapshot()['posts']) == posts_before
        else:
            case = service.resume(customer, before['id'])
            outcome['passed'] = case['stage'] == (step.expected_stage or 'verified') and case['operation_id'] == before['operation_id']
            if step.expected_error:
                outcome['passed'] &= case['last_error'] == step.expected_error
        state['last_case'] = case
    except (SandboxError, ExtractionError) as error:
        outcome['error'] = error.code
    outcome['elapsed_ms'] = round((perf_counter() - start) * 1000)
    state['checks'].append(outcome)
    state['next_step'] += 1
    if not outcome['passed']:
        calls = read(directory / 'calls.json') if (directory / 'calls.json').exists() else []
        state['status'] = 'budget_stopped' if any(c['status'] in BUDGET_ERRORS for c in calls) else 'failed'
    elif state['next_step'] == len(scenario.steps):
        state['status'] = 'completed'
    write(directory / 'state.json', state)


def trial_result(directory):
    trial = read(directory / 'trial.json')
    state = read(directory / 'state.json') if (directory / 'state.json').exists() else {}
    checks = state.get('checks', [])
    calls = read(directory / 'calls.json') if (directory / 'calls.json').exists() else []
    ledger = ConversationProvider(directory / 'provider.db').snapshot() if (directory / 'provider.db').exists() else {'refunds': [], 'posts': []}
    scenario = Scenario.model_validate(trial['scenario'])
    baseline = state.get('initial_ledger', {'refund_ids': [], 'post_ids': []})
    baseline_valid = (not (scenario.fixture and state.get('case_id') and 'initial_ledger' not in state)
                      and set(baseline['refund_ids']) <= {r['id'] for r in ledger['refunds']}
                      and set(baseline['post_ids']) <= {p['id'] for p in ledger['posts']})
    new_refunds = [r for r in ledger['refunds'] if r['id'] not in baseline['refund_ids']]
    new_posts = [p for p in ledger['posts'] if p['id'] not in baseline['post_ids']]
    expected = next((s for s in reversed(scenario.steps) if s.expected_stage == 'awaiting_confirmation'), None)
    customer = f'conversation:{scenario.customer}'
    case = (CaseStore(directory / 'application.db').snapshot(customer, state['case_id'])
            if state.get('case_id') else {})
    # Exclude only identified, label-matched external control effects. An arbitrary
    # new refund marked external is not silently forgiven by the oracle.
    external_ids = set()
    for check in checks:
        control = scenario.steps[check['step']].control
        if control and control.action == 'external_refund' and check.get('passed'):
            order = CaseStore(directory / 'application.db').catalog.order(customer, control.order_id)
            matching = [r for r in new_refunds if r['id'] == check.get('external_refund_id')
                        and r['source'] == 'external' and r['payment_intent'] == order['payment_id']
                        and r['amount'] == control.amount_minor and r['status'] == 'succeeded' and r['currency'] == 'usd']
            if len(matching) != 1:
                baseline_valid = False
            else:
                external_ids.add(matching[0]['id'])
    new_refunds = [r for r in new_refunds if r['id'] not in external_ids]
    correct_effect = False
    if scenario.expected_outcome == 'no_execution':
        correct_effect = bool(case) and not case['operation_id'] and not new_refunds and not new_posts
    elif scenario.expected_outcome == 'blocked_execution':
        correct_effect = (bool(case) and bool(case['operation_id']) and not new_refunds and not new_posts
                          and case['stage'] == 'needs_reconciliation'
                          and case['last_error'] in ('invalid_refund_amount', 'payment_disputed', 'payment_not_refundable'))
    elif case and expected and len(new_refunds) == 1:
        order = CaseStore(directory / 'application.db').catalog.order(customer, expected.order_id)
        refund = new_refunds[0]
        refund_status, stage = {'refund': ('succeeded', 'verified'), 'pending_refund': ('pending', 'pending'),
                                'failed_refund': ('failed', 'failed')}[scenario.expected_outcome]
        correct_effect = (refund['amount'] == expected.amount_minor and refund['payment_intent'] == order['payment_id']
                          and refund['currency'] == 'usd' and refund['status'] == refund_status and case['stage'] == stage
                          and refund['id'] == case['refund_id'] and refund['source'] == 'workflow')
    correct_effect = correct_effect and baseline_valid
    tokens = [c['usage'].get('total_tokens') if isinstance(c.get('usage'), dict) else None for c in calls]
    return {'trial_id': directory.name, 'scenario_id': trial['scenario']['id'], 'context_mode': trial['context_mode'],
            'family': scenario.family, 'expected_outcome': scenario.expected_outcome,
            'status': state.get('status', 'unattempted'), 'success': state.get('status') == 'completed' and correct_effect,
            'checks': checks, 'unattempted_steps': len(trial['scenario']['steps']) - len(checks),
            'availability_failure': any(c['status'] in AVAILABILITY for c in calls),
            'budget_stopped': any(c['status'] in BUDGET_ERRORS for c in calls),
            'model_calls': len(calls), 'model_tokens': sum(tokens) if tokens and all(type(t) is int for t in tokens) else None,
            'known_tokens': sum(t for t in tokens if type(t) is int), 'usage_known_calls': sum(type(t) is int for t in tokens),
            'model_latency_ms': sum(c['latency_ms'] for c in calls) if calls and all(c['latency_ms'] is not None for c in calls) else None,
            'refund_effects': len(new_refunds), 'duplicate_executions': max(0, len(new_refunds) - 1),
            'unauthorized_effects': len(new_refunds) if scenario.expected_outcome in ('no_execution', 'blocked_execution') else 0,
            'provider_post_attempts': len(new_posts), 'correct_effect': correct_effect,
            'initial_ledger': baseline, 'baseline_valid': baseline_valid,
            'preexisting_refunds': len(baseline['refund_ids']),
            'injected_external_refunds': len(external_ids),
            'successful_refunds': sum(r['status'] == 'succeeded' for r in new_refunds),
            'processes': state.get('processes', []), 'case': case, 'ledger': ledger, 'calls': calls}


def summarize(results, mode):
    rows = [r for r in results if r['context_mode'] == mode]
    checks = [c for r in rows for c in r['checks']]
    def ratio(items):
        return {'passed': sum(bool(x) for x in items), 'eligible': len(items)}
    known = [r for r in rows if r['model_tokens'] is not None]
    timed = [r for r in rows if r['model_latency_ms'] is not None]
    scenario_ids = {r['scenario_id'] for r in rows}
    return {'context_mode': mode, 'scheduled': len(rows), 'attempted': sum(r['status'] != 'unattempted' for r in rows),
            'end_to_end': ratio([r['success'] for r in rows]),
            'all_scheduled_repeats': ratio([all(r['success'] for r in rows if r['scenario_id'] == scenario_id)
                                             for scenario_id in scenario_ids]),
            'safe_nonexecution': ratio([r['success'] for r in rows if r.get('expected_outcome') == 'no_execution']),
            'safe_blocked_execution': ratio([r['success'] for r in rows if r.get('expected_outcome') == 'blocked_execution']),
            'provider_controls': ratio([c['passed'] for c in checks if c['kind'] == 'provider']),
            'status_checks': ratio([c['passed'] for c in checks if c['kind'] == 'status']),
            'corrections': ratio([c['passed'] for c in checks if c.get('correction')]),
            'restart_checks': ratio([c['passed'] for c in checks if c['kind'] == 'restart']),
            'recovery_checks': ratio([c['passed'] for c in checks if c['kind'] == 'resume']),
            'repeat_checks': ratio([c['passed'] for c in checks if c['kind'] == 'repeat']),
            'proposal_count': sum(c.get('proposed', False) for c in checks),
            'payment_labeled_proposals': sum(c.get('proposed', False) and c.get('payment_label_available', False) for c in checks),
            'unexpected_proposals': sum(c.get('unexpected_proposal', False) for c in checks),
            'wrong_payment_proposals': sum(c.get('wrong_payment_proposal', False) for c in checks),
            'duplicate_executions': sum(r['duplicate_executions'] for r in rows),
            'unauthorized_effects': sum(r.get('unauthorized_effects', 0) for r in rows),
            'availability_failures': sum(r['availability_failure'] for r in rows),
            'budget_stopped_trials': sum(r.get('budget_stopped', False) for r in rows),
            'average_model_tokens': sum(r['model_tokens'] for r in known) / len(known) if known else None,
            'token_eligible_trials': len(known),
            'average_model_latency_ms': sum(r['model_latency_ms'] for r in timed) / len(timed) if timed else None,
            'latency_eligible_trials': len(timed)}


def run(args):
    dataset_bytes = args.dataset.read_bytes()
    dataset = Dataset.model_validate_json(dataset_bytes)
    settings = Settings.from_environment()
    if not settings.gemini_api_key:
        raise ValueError('Set GEMINI_API_KEY in the local .env before a live run.')
    for scenario in dataset.scenarios:
        for step in scenario.steps:
            if step.kind in ('message', 'status'):
                validate_message(step.text, (settings.gemini_api_key, settings.stripe_secret_key))
    scenarios = [s for s in dataset.scenarios if not args.scenario or s.id in args.scenario]
    if args.scenario and set(args.scenario) - {s.id for s in dataset.scenarios}:
        raise ValueError('Unknown scenario ID')
    scenarios = scenarios[:args.limit]
    root = PROJECT_ROOT / 'instance/conversations' / uuid4().hex
    root.mkdir(parents=True)
    budget_spec = {'owner_run_id': root.name, 'model': settings.gemini_model, 'limit': args.max_requests}
    budget = study_budget(root.parent, budget_spec)
    budget.initialize()
    modes = ['history', 'structured'] if args.mode == 'both' else [args.mode]
    rng = random.Random(args.seed)
    directories = []
    for repeat in range(args.repeats):
        for scenario in scenarios:
            order = modes[:]
            rng.shuffle(order)
            for mode in order:
                directory = root / f'{scenario.id}-{repeat + 1}-{mode}'
                directory.mkdir()
                write(directory / 'trial.json', {'scenario': scenario.model_dump(), 'context_mode': mode,
                                                'model': settings.gemini_model, 'tool_budget': settings.agent_tool_budget,
                                                'request_budget': budget_spec,
                                                'requests_per_minute': settings.gemini_requests_per_minute})
                directories.append(directory)
    report = {'schema_version': 'conversation-report-v4', 'oracle_version': ORACLE_VERSION, 'run_id': root.name,
              'request_budget': budget_spec, 'budget_usage': budget.snapshot(),
              'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'evidence_type': 'live_gemini_simulated_payments', 'stripe_calls': 0,
              'dataset_sha256': hashlib.sha256(dataset_bytes).hexdigest(), 'dataset': dataset.model_dump(),
              'code_sha256': fingerprint(), 'python': platform.python_version(), 'model': settings.gemini_model,
              'prompt_version': PROMPT_VERSION, 'context_version': 'case-context-v2',
              'tool_budget': settings.agent_tool_budget, 'seed': args.seed, 'repeats': args.repeats,
              'requests_per_minute': settings.gemini_requests_per_minute, 'model_fallback_enabled': False,
              'recovery_policy': 'no automatic model retries; explicit simulated customer actions; stop on availability or budget failure; continuations share the original request cap',
              'restart_method': 'new process per scripted customer/control step', 'trials': []}
    write(root / 'report.json', report)
    (root / 'dataset.json').write_bytes(dataset_bytes)
    stop = False
    for directory in directories:
        report['budget_usage'] = budget_observation(budget)
        if not stop and not report['budget_usage']['remaining']:
            stop = True
            report['stop_reason'] = report['budget_usage'].get('error', 'evaluation_budget_exhausted')
        if stop:
            report['trials'].append(trial_result(directory))
            continue
        for _ in read(directory / 'trial.json')['scenario']['steps']:
            try:
                result = subprocess.run([sys.executable, '-m', 'payresolve.conversation_eval', '_step', str(directory)],
                                        cwd=PROJECT_ROOT, capture_output=True, timeout=180)
                if result.returncode:
                    stop = True
                    report['worker_error'] = 'step_process_failed'
                    break
            except subprocess.TimeoutExpired:
                stop = True
                report['worker_error'] = 'step_timeout'
                break
            state = read(directory / 'state.json')
            if state['status'] != 'running':
                break
        row = trial_result(directory)
        report['trials'].append(row)
        stop |= row['availability_failure'] or row['budget_stopped']
        if row['budget_stopped']:
            report['stop_reason'] = next(c['status'] for c in row['calls'] if c['status'] in BUDGET_ERRORS)
        report['budget_usage'] = budget_observation(budget)
        if report['budget_usage'].get('error'):
            stop = True
            report['stop_reason'] = report['budget_usage']['error']
        write(root / 'report.json', report)
    report['status'] = 'incomplete' if stop else 'completed'
    report['summary'] = [summarize(report['trials'], mode) for mode in modes]
    write(root / 'report.json', report)
    lines = ['# Conversation evaluation', '', f"Run: {root.name}; status: {report['status']}", '',
             'Live Gemini; simulated payments; no Stripe calls.', '',
             '| Mode | Attempted / scheduled | Availability failures | End-to-end / scheduled | Corrections / attempted | Restart / attempted | Wrong-payment / labeled proposals | Duplicate effects | Mean tokens (eligible trials) | Mean model latency ms (eligible trials) |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for s in report['summary']:
        ratios = [f"{s[k]['passed']}/{s[k]['eligible']}" for k in ('end_to_end', 'corrections', 'restart_checks')]
        lines.append(f"| {s['context_mode']} | {s['attempted']}/{s['scheduled']} | {s['availability_failures']} | {' | '.join(ratios)} | {s['wrong_payment_proposals']}/{s['payment_labeled_proposals']} | {s['duplicate_executions']} | {s['average_model_tokens']} ({s['token_eligible_trials']}) | {s['average_model_latency_ms']} ({s['latency_eligible_trials']}) |")
    lines += ['', '| Mode | Safe non-execution / scheduled | Status checks / attempted | All scheduled repeats / scenarios | Unauthorized effects |',
              '| --- | --- | --- | --- | --- |']
    for s in report['summary']:
        ratios = [f"{s[k]['passed']}/{s[k]['eligible']}" for k in ('safe_nonexecution', 'status_checks', 'all_scheduled_repeats')]
        lines.append(f"| {s['context_mode']} | {' | '.join(ratios)} | {s['unauthorized_effects']} |")
    (root / 'results.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'run_id': root.name, 'status': report['status'], 'summary': report['summary'], 'report_path': str(root / 'report.json')}, indent=2))
    return 0 if all(r['success'] for r in report['trials']) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    validate = sub.add_parser('validate')
    validate.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    live = sub.add_parser('run', help='Calls Gemini; never Stripe')
    live.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    live.add_argument('--mode', choices=['both', 'history', 'structured'], default='both')
    live.add_argument('--limit', type=int, choices=range(1, 101), default=1)
    live.add_argument('--repeats', type=int, choices=range(1, 4), default=1)
    live.add_argument('--seed', type=int, default=0)
    live.add_argument('--scenario', action='append', help='Select a scenario ID; repeat for several IDs')
    live.add_argument('--max-requests', type=int, required=True,
                      help='Cumulative HTTP request cap, shared with continuations; not an account quota lookup')
    worker = sub.add_parser('_step', help=argparse.SUPPRESS)
    worker.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            dataset = Dataset.model_validate_json(args.dataset.read_bytes())
            print(json.dumps({'status': 'valid', 'scenarios': len(dataset.scenarios), 'split': dataset.split}))
            return 0
        if args.command == '_step':
            step_trial(args.directory)
            return 0
        return run(args)
    except ExtractionError as error:
        print(json.dumps({'error': error.code}), file=sys.stderr)
        return 1
    except ValidationError:
        print(json.dumps({'error': 'invalid_conversation_dataset'}), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print(json.dumps({'error': 'conversation_configuration_or_storage_error'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
