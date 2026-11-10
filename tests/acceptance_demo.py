"""Reproducible HTTP acceptance demo with scripted tools and simulated payments.

Run: python -m tests.acceptance_demo
No .env loading, network calls, or live model-quality measurements.
Each phase runs in a new Python process using saved app/provider state.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import uuid4

from payresolve import create_app
from payresolve.automation import CaseAutomation
from payresolve.cases import CaseStore
from payresolve.catalog import seed_catalog
from payresolve.config import PROJECT_ROOT, Settings
from tests.test_agent import ScriptedPlanner, proposal_action
from tests.test_workflow import FakeStripe

SCENARIO = PROJECT_ROOT / 'evals' / 'conversations' / 'acceptance.json'
PROVIDER_FIELDS = ('payments', 'refunds', 'keys', 'refund_posts', 'lose_response',
                   'fail_refund_read', 'refund_status')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def code_fingerprint():
    paths = [p for p in (PROJECT_ROOT / 'payresolve').rglob('*')
             if p.suffix in ('.py', '.html', '.css', '.js')]
    paths += [Path(__file__), PROJECT_ROOT / 'tests/test_agent.py',
              PROJECT_ROOT / 'tests/test_workflow.py', PROJECT_ROOT / 'requirements.txt']
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def phase(directory, mode, name):
    scenario = read(SCENARIO)
    settings = replace(Settings(directory / 'application.db'), agent_context_mode=mode)
    gateway = FakeStripe()
    state_path = directory / 'state.json'
    saved = read(state_path) if state_path.exists() else {'checks': [], 'processes': []}
    if 'provider' in saved:
        for key, value in saved['provider'].items():
            setattr(gateway, key, value)
    saved['processes'].append({'phase': name, 'pid': os.getpid()})
    store = CaseStore(settings.database_path)
    if name == 'prepare':
        seed_catalog(store.catalog, gateway, 'acceptance')
    app = create_app(settings)
    client = app.test_client()
    client.get('/demo')
    with client.session_transaction() as session:
        token = session['csrf']
    selected = client.post('/demo/customer', data={'csrf': token, 'customer_id': 'acceptance:a'})

    def check(label, condition):
        saved['checks'].append({'phase': name, 'check': label, 'passed': bool(condition)})
        if not condition:
            write(state_path, saved)
            raise RuntimeError('Acceptance check failed: ' + label)

    check('demo_customer_selected', selected.status_code == 303)

    def snapshot(case_id):
        return store.snapshot('acceptance:a', case_id)

    def post(case, action, **data):
        return client.post(f"/demo/cases/{case['id']}/{action}", data={
            'csrf': token, 'version': case['version'], **data})

    def chat_proposal():
        planner = ScriptedPlanner([
            {'action': 'search_orders', 'query': 'headphones', 'last_week': True},
            {'action': 'ask', 'reason': 'purchase'},
            {'action': 'inspect_payment', 'order_id': 'ORD-102'}, proposal_action])
        with patch('payresolve.dashboard.GeminiPlanner', return_value=planner):
            response = client.post('/demo/cases', data={'csrf': token, 'message': scenario['initial_message']})
            check('chat_creation_redirect', response.status_code == 303)
            case = snapshot(response.headers['Location'].rsplit('/', 1)[-1])
            check('ambiguity_requests_clarification', case['stage'] == 'awaiting_clarification'
                  and sorted(case['candidates']) == scenario['expected_candidates'])
            response = post(case, 'message', message=scenario['selection_message'])
            check('short_reply_redirect', response.status_code == 303)
        case = snapshot(case['id'])
        expected = scenario['initial_proposal']
        check('short_reply_proposal', case['stage'] == 'awaiting_confirmation'
              and case['selected_order'] == expected['order_id']
              and case['amount_minor'] == expected['amount_minor'] and case['currency'] == expected['currency'])
        check('no_automatic_confirmation', case['operation_id'] is None)
        return case

    with patch('payresolve.dashboard.StripeSandbox', return_value=gateway):
        if name == 'prepare':
            case = chat_proposal()
            check('no_refund_before_confirmation', not gateway.refund_posts)
            saved['first_card'] = case
        elif name == 'confirm':
            card = saved['first_card']
            case = snapshot(card['id'])
            check('proposal_and_messages_survive_process_exit', case == card)
            check('reopened_page_has_confirmation', b'Confirm refund' in client.get(f"/demo/cases/{case['id']}").data)
            gateway.lose_response = True
            response = post(card, 'confirm', revision=card['revision'], confirm='yes')
            check('confirmed_http_redirect', response.status_code == 303)
            case = snapshot(card['id'])
            check('lost_response_honestly_unverified', case['stage'] == 'needs_reconciliation'
                  and case['operation_id'] is not None and len(gateway.refunds) == 1)
            saved['claimed_operation'] = case['operation_id']
        else:
            card = saved['first_card']
            clock = [time.time()]
            worker = CaseAutomation(settings, gateway_factory=lambda: gateway, clock=lambda: clock[0])
            worker.tick()
            clock[0] += 6  # Fast-forward scheduling only; real replay-window checks remain active.
            worker.tick()
            case = snapshot(card['id'])
            check('automatic_recovery_after_process_restart', case['stage'] == 'verified'
                  and case['operation_id'] == saved['claimed_operation'])
            check('same_key_one_effect', len(gateway.refund_posts) == 2 and len(gateway.refunds) == 1
                  and gateway.refund_posts[0][1] == gateway.refund_posts[1][1])
            response = post(card, 'confirm', revision=card['revision'], confirm='yes')
            repeated = snapshot(card['id'])
            check('duplicate_http_confirmation_returns_existing_result', response.status_code == 303
                  and repeated['refund_id'] == case['refund_id'] and len(gateway.refund_posts) == 2)
            with client.session_transaction() as session:
                session['demo_customer_id'] = 'acceptance:b'
            check('other_customer_cannot_download_case', client.get(f"/demo/cases/{card['id']}/record").status_code == 404)
            check('other_customer_cannot_confirm_case', post(card, 'confirm', revision=card['revision'], confirm='yes').status_code == 404)
            with client.session_transaction() as session:
                session['demo_customer_id'] = 'acceptance:a'

            old = chat_proposal()
            corrected_action = lambda context: proposal_action(context, amount='5', amount_quote='$5',
                                                               order='ORD-101', order_quote='Monday')
            with patch('payresolve.dashboard.GeminiPlanner', return_value=ScriptedPlanner([
                    {'action': 'inspect_payment', 'order_id': 'ORD-101'}, corrected_action])):
                check('correction_accepted', post(old, 'message', message=scenario['correction_message']).status_code == 303)
            revised = snapshot(old['id'])
            expected = scenario['corrected_proposal']
            check('amount_and_purchase_revised', revised['revision'] == old['revision'] + 1
                  and revised['selected_order'] == expected['order_id'] and revised['amount_minor'] == expected['amount_minor']
                  and revised['currency'] == expected['currency'] and revised['operation_id'] is None)
            response = post(old, 'confirm', revision=old['revision'], confirm='yes')
            check('stale_confirmation_rejected_without_post', response.status_code == 409 and len(gateway.refund_posts) == 2)
            check('revised_confirmation_accepted', post(revised, 'confirm', revision=revised['revision'], confirm='yes').status_code == 303)
            final = snapshot(old['id'])
            check('corrected_refund_verified', final['stage'] == 'verified'
                  and gateway.refunds[final['refund_id']]['amount'] == expected['amount_minor']
                  and gateway.refunds[final['refund_id']]['payment_intent'] == store.catalog.order('acceptance:a', expected['order_id'])['payment_id'])
            check('two_confirmed_cases_two_effects', len(gateway.refunds) == 2 and len(gateway.refund_posts) == 3)
            saved['case_records'] = []
            for case_id in (card['id'], old['id']):
                response = client.get(f'/demo/cases/{case_id}/record')
                check('case_record_downloaded', response.status_code == 200)
                record = response.get_json()
                record['experiment_label'] = 'scripted_model_simulated_payments'
                saved['case_records'].append(record)
    saved['provider'] = {key: getattr(gateway, key) for key in PROVIDER_FIELDS}
    write(state_path, saved)


def run(modes):
    run_id = uuid4().hex
    directory = PROJECT_ROOT / 'instance' / 'acceptance' / run_id
    directory.mkdir(parents=True)
    report = {'run_id': run_id, 'schema_version': 'acceptance-report-v1',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'scenario_id': read(SCENARIO)['id'], 'scenario_sha256': hashlib.sha256(SCENARIO.read_bytes()).hexdigest(),
              'code_sha256': code_fingerprint(), 'python': platform.python_version(),
              'evidence_type': 'scripted_model_simulated_payments', 'live_provider_calls': 0,
              'model_quality_measured': False, 'token_usage': None,
              'restart_method': 'separate Python processes with persisted app and simulator state',
              'arms': [], 'status': 'completed'}
    for mode in modes:
        arm_dir = directory / mode
        arm_dir.mkdir()
        for name in read(SCENARIO)['phases']:
            try:
                result = subprocess.run([sys.executable, '-m', 'tests.acceptance_demo', '--phase', name,
                                         '--directory', str(arm_dir), '--context-mode', mode],
                                        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                report['status'] = 'failed'
                report['failed_phase'] = {'mode': mode, 'phase': name, 'error': 'phase_timeout'}
                break
            if result.returncode:
                report['status'] = 'failed'
                report['failed_phase'] = {'mode': mode, 'phase': name, 'exit_code': result.returncode}
                (arm_dir / 'failure.txt').write_text(result.stderr, encoding='utf-8')
                break
        saved = read(arm_dir / 'state.json') if (arm_dir / 'state.json').exists() else {}
        checks = saved.get('checks', [])
        report['arms'].append({'context_mode': mode, 'checks': checks, 'processes': saved.get('processes', []),
                               'case_records': saved.get('case_records', []),
                               'passed': sum(c['passed'] for c in checks), 'checked': len(checks)})
        if report['status'] == 'failed':
            break
    write(directory / 'report.json', report)
    print(json.dumps({'run_id': run_id, 'status': report['status'],
                      'evidence_type': report['evidence_type'],
                      'arms': [{k: arm[k] for k in ('context_mode', 'passed', 'checked')} for arm in report['arms']],
                      'report_path': str(directory / 'report.json')}, indent=2))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context-mode', choices=('structured', 'history', 'both'), default='both')
    parser.add_argument('--phase', choices=('prepare', 'confirm', 'recover'), help=argparse.SUPPRESS)
    parser.add_argument('--directory', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phase:
        if args.directory is None or args.context_mode == 'both':
            parser.error('Internal phase requires a directory and one context mode')
        phase(args.directory, args.context_mode, args.phase)
    else:
        sys.exit(run(['history', 'structured'] if args.context_mode == 'both' else [args.context_mode]))
