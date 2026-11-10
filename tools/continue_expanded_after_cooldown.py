"""Resume one E48 block's untouched trials under the declared post-stop addendum.

This command does not replay attempted trials. It waits for the declared retry
time and any active local provider cooldown. A documented billing change may
declare an earlier retry time; a new 429 still ends this invocation.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.continue_conversation_study import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--block', required=True, choices=('primary', 'backup'))
    parser.add_argument('--source-run', help='Use a later saved continuation run ID')
    args = parser.parse_args()
    protocol = json.loads((ROOT/'evals/conversations/protocols/expanded-continuation-v3.json').read_text())
    freeze = json.loads((ROOT/protocol['original_freeze']).read_text())
    baseline = ROOT/protocol['original_export']
    if sha256(baseline.read_bytes()).hexdigest() != protocol['original_export_sha256']:
        raise ValueError('Original E48 export changed; collection refused.')
    block = next(item for item in freeze['blocks'] if item['name'] == args.block)
    source_id = args.source_run or protocol['source_runs'][args.block]
    origin_id = protocol['source_runs'][args.block]
    path = ROOT/'instance/conversations'/source_id/'report.json'
    report = json.loads(path.read_text())
    cursor = report
    while cursor['run_id'] != origin_id:
        parent = cursor.get('parent_run_id')
        if not parent:
            raise ValueError('Source run is outside this block continuation chain.')
        cursor = json.loads((ROOT/'instance/conversations'/parent/'report.json').read_text())
    if (report['status'] == 'running' or report['model'] != block['model'] or
            report['code_sha256'] != freeze['code_sha256'] or
            report['dataset_sha256'] != block['dataset_sha256']):
        raise ValueError('Source run is active or differs from the frozen block.')
    remaining = sum(row['status'] == 'unattempted' for row in report['trials'])
    if not remaining:
        print(json.dumps({'status':'all_trials_attempted','source_run_id':source_id}))
        return 0
    retry_time = protocol.get('billing_change', {}).get(
        'authorized_retry_utc', protocol['earliest_provider_cooldown_utc'])
    earliest = datetime.fromisoformat(retry_time).timestamp()
    if time.time() < earliest:
        print(json.dumps({'status':'provider_cooldown_active',
                          'earliest_utc':retry_time,
                          'untouched_trials':remaining}))
        return 2
    pacing = ROOT/'instance/model-pacing.db'
    if pacing.exists():
        with sqlite3.connect(pacing) as connection:
            row = connection.execute('SELECT until FROM model_pacing WHERE name=?', (block['model'],)).fetchone()
        if row and time.time() < row[0]:
            print(json.dumps({'status':'provider_cooldown_active',
                              'earliest_utc':datetime.fromtimestamp(row[0],timezone.utc).isoformat(),
                              'untouched_trials':remaining}))
            return 2
    os.environ.update(GEMINI_MODEL=block['model'],
                      GEMINI_REQUESTS_PER_MINUTE=str(freeze['rpm']),
                      AGENT_TOOL_BUDGET=str(freeze['tool_budget']))
    created = []
    result = run(source_id, created=created.append)
    if created:
        latest = json.loads((created[0]/'report.json').read_text())
        print(json.dumps({'status':latest['status'],'run_id':latest['run_id'],
                          'untouched_trials':sum(row['status']=='unattempted' for row in latest['trials']),
                          'attempted_trials':sum(row['status']!='unattempted' for row in latest['trials'])}))
    return result


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error) as error:
        raise SystemExit(f'Post-stop continuation refused: {type(error).__name__}')
