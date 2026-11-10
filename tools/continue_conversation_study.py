"""Continue only untouched trials of a frozen study, preserving the original run.

Uses the unchanged application's stepper, oracle and metrics. Failed, completed
and partially started trials are never rerun. This driver is hashed separately.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from payresolve.config import PROJECT_ROOT, Settings
from payresolve.conversation_eval import Dataset, fingerprint, read, write, trial_result, summarize
from payresolve.evaluation_budget import BUDGET_ERRORS, budget_observation, study_budget


def untouched_trials(source, report):
    selected = []
    for row in report['trials']:
        if row['status'] != 'unattempted':
            continue
        trial_id = row['trial_id']
        if not re.fullmatch(r'[a-z0-9-]+', trial_id):
            raise ValueError('Invalid trial identifier.')
        directory = source / trial_id
        # Even a crash before state.json could have started fixture/provider work.
        if {p.name for p in directory.iterdir()} != {'trial.json'}:
            raise ValueError('An unattempted trial has unexpected artifacts; inspect before continuing.')
        selected.append(directory)
    return selected


def run(source_id, *, created=None):
    if not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError('Provide a saved run ID.')
    source = PROJECT_ROOT / 'instance/conversations' / source_id
    original = read(source / 'report.json')
    settings = Settings.from_environment()
    if (original['status'] != 'incomplete' or original['code_sha256'] != fingerprint()
            or original['model'] != settings.gemini_model
            or original['tool_budget'] != settings.agent_tool_budget
            or original['requests_per_minute'] != settings.gemini_requests_per_minute
            or original.get('model_fallback_enabled') is not False
            or hashlib.sha256((source / 'dataset.json').read_bytes()).hexdigest() != original['dataset_sha256']):
        raise ValueError('Frozen source, model, dataset or runtime settings do not match.')
    if 'request_budget' not in original:
        raise ValueError('Use the archived runner for legacy studies without a frozen request budget.')
    budget = study_budget(source.parent, original['request_budget'])
    if budget.model != original['model'] or not budget_observation(budget)['remaining']:
        raise ValueError('The original request budget is exhausted, unavailable or mismatched.')
    untouched = untouched_trials(source, original)
    if not untouched:
        raise ValueError('No untouched trials remain.')
    canonical = Dataset.model_validate_json((source / 'dataset.json').read_bytes()).model_dump()
    if canonical != original['dataset']:
        raise ValueError('Saved dataset metadata differs from the frozen labels.')
    labels = {scenario['id']: scenario for scenario in canonical['scenarios']}
    rows = {row['trial_id']: row for row in original['trials']}
    for directory in untouched:
        trial = read(directory / 'trial.json')
        if (trial['scenario'] != labels[rows[directory.name]['scenario_id']]
                or trial['context_mode'] != rows[directory.name]['context_mode']
                or trial['model'] != original['model'] or trial['tool_budget'] != original['tool_budget']
                or trial['requests_per_minute'] != original['requests_per_minute']
                or trial.get('request_budget') != original['request_budget']):
            raise ValueError('A saved trial differs from frozen settings or labels.')
    root = source.parent / uuid4().hex
    root.mkdir()
    (root / 'dataset.json').write_bytes((source / 'dataset.json').read_bytes())
    report = deepcopy(original)
    report.update(run_id=root.name, parent_run_id=source_id, status='running',
                  created_at=datetime.now(timezone.utc).isoformat(),
                  continuation_driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  continuation_policy='Only previously untouched trials; inherited failures/interruption retained; same frozen application and trial order; stop on availability or worker failure.',
                  continued_trial_ids=[p.name for p in untouched], worker_errors=[])
    report.pop('worker_error', None)
    report.pop('summary', None)
    report.pop('stop_reason', None)
    report['budget_usage'] = budget_observation(budget)
    for row in report['trials']:
        row.setdefault('source_run_id', source_id)
        if row['status'] == 'running':
            row.update(status='interrupted', original_status='running')
    for directory in untouched:
        target = root / directory.name
        target.mkdir()
        (target / 'trial.json').write_bytes((directory / 'trial.json').read_bytes())
    write(root / 'report.json', report)
    if created is not None:
        created(root)
    secrets = (settings.gemini_api_key, settings.stripe_secret_key)
    stopped = False
    for directory in untouched:
        target = root / directory.name
        report['budget_usage'] = budget_observation(budget)
        if not report['budget_usage']['remaining']:
            stopped = True
            report['stop_reason'] = report['budget_usage'].get('error', 'evaluation_budget_exhausted')
        if stopped:
            break
        for _ in read(target / 'trial.json')['scenario']['steps']:
            if fingerprint() != original['code_sha256']:
                raise ValueError('Application source changed during continuation.')
            try:
                result = subprocess.run([sys.executable, '-m', 'payresolve.conversation_eval', '_step', str(target)],
                                        cwd=PROJECT_ROOT, capture_output=True, timeout=180)
                if result.returncode:
                    diagnostic = {'trial_id': target.name, 'returncode': result.returncode,
                                  'stderr': result.stderr.decode('utf-8', errors='replace')[-8000:],
                                  'stdout': result.stdout.decode('utf-8', errors='replace')[-2000:]}
                    for key in ('stderr', 'stdout'):
                        for secret in secrets:
                            if secret:
                                diagnostic[key] = diagnostic[key].replace(secret, '[REDACTED]')
                    write(target / 'worker-error.json', diagnostic)
                    report['worker_errors'].append({'trial_id': target.name, 'returncode': result.returncode})
                    stopped = True
                    break
            except subprocess.TimeoutExpired:
                report['worker_errors'].append({'trial_id': target.name, 'error': 'step_timeout'})
                stopped = True
                break
            if read(target / 'state.json')['status'] != 'running':
                break
        row = trial_result(target)
        row['source_run_id'] = root.name
        report['trials'] = [row if previous['trial_id'] == row['trial_id'] else previous for previous in report['trials']]
        stopped |= row['availability_failure'] or row.get('budget_stopped', False)
        if row.get('budget_stopped'):
            report['stop_reason'] = next(c['status'] for c in row['calls'] if c['status'] in BUDGET_ERRORS)
        report['budget_usage'] = budget_observation(budget)
        if report['budget_usage'].get('error'):
            stopped = True
            report['stop_reason'] = report['budget_usage']['error']
        write(root / 'report.json', report)
        print(f"{root.name}: {row['trial_id']} {row['status']} success={row['success']}", flush=True)
    # A completed continuation does not repair an inherited interrupted trial.
    report['continuation_status'] = 'incomplete' if stopped else 'completed'
    report['status'] = 'incomplete' if stopped or any(r['status'] in ('running', 'interrupted', 'unattempted', 'budget_stopped') for r in report['trials']) else 'completed'
    report['summary'] = [summarize(report['trials'], mode) for mode in ('history', 'structured')]
    write(root / 'report.json', report)
    lines = ['# Frozen study continuation', '', f'Run: {root.name}; parent: {source_id}', '',
             'Cumulative results retain original failures and interruption; no original attempted trial was rerun.', '',
             '| Mode | Attempted / scheduled | End-to-end / scheduled | Wrong-payment proposals | Duplicate effects |',
             '| --- | --- | --- | --- | --- |']
    for summary in report['summary']:
        lines.append(f"| {summary['context_mode']} | {summary['attempted']}/{summary['scheduled']} | {summary['end_to_end']['passed']}/{summary['end_to_end']['eligible']} | {summary['wrong_payment_proposals']}/{summary['payment_labeled_proposals']} | {summary['duplicate_executions']} |")
    (root / 'results.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"Saved {root / 'report.json'}", flush=True)
    return 0 if not stopped else 1


def finish_untouched(source_id):
    """Collect remaining trials, pausing after service failures, never replaying.

    A quota/access/configuration or worker failure stops for investigation.
    The number of remaining original trials strictly decreases per segment.
    """
    if not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError('Provide a saved run ID.')
    while True:
        path = PROJECT_ROOT / 'instance/conversations' / source_id / 'report.json'
        report = read(path)
        remaining = sum(row['status'] == 'unattempted' for row in report['trials'])
        if not remaining:
            if any(row['status'] == 'budget_stopped' for row in report['trials']):
                print(f'Collection retains a budget-interrupted trial: {path}', flush=True)
                return 1
            print(f'All scheduled trials attempted; retained failures/interruption are in {path}', flush=True)
            return 0
        if report.get('request_budget'):
            budget = study_budget(path.parent.parent, report['request_budget'])
            if not budget_observation(budget)['remaining']:
                print(f'Collection stopped at the original request budget: {path}', flush=True)
                return 1
        current_calls = [call['status'] for row in report['trials'] if row.get('source_run_id', source_id) == source_id
                         for call in row.get('calls', [])]
        if (report.get('worker_errors') or report.get('worker_error') or
                set(current_calls) & ({'gemini_rate_limited', 'gemini_access_denied', 'gemini_request_rejected'} | BUDGET_ERRORS)):
            print(f'Collection stopped for quota/access/configuration or worker failure: {path}', flush=True)
            return 1
        if set(current_calls) & {'gemini_service_unavailable', 'gemini_unavailable'}:
            delay = max(0, 60 - (time.time() - path.stat().st_mtime))
            if delay:
                print(f'Waiting {delay:.0f}s after provider failure; {remaining} untouched trials remain.', flush=True)
                time.sleep(delay)
        created = []
        run(source_id, created=created.append)
        if not created:
            raise ValueError('Continuation produced no checkpoint.')
        source_id = created[0].name
        after = read(created[0] / 'report.json')
        if sum(row['status'] == 'unattempted' for row in after['trials']) >= remaining:
            print('Collection made no progress; inspect the saved worker diagnostics.', flush=True)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--finish-untouched', action='store_true',
                        help='Continue untouched trials after service failures with a 60-second pause; stop on quota or worker failures')
    args = parser.parse_args()
    try:
        return finish_untouched(args.source_run) if args.finish_untouched else run(args.source_run)
    except (ValueError, OSError) as error:
        print(f'Continuation stopped: {type(error).__name__}; check saved artifacts and frozen settings.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
