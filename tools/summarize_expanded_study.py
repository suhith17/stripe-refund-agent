"""Export frozen expanded-study outcomes without raw conversations or credentials."""
from collections import Counter, defaultdict
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from payresolve.evaluation_budget import study_budget, budget_observation


def counters(rows):
    attempted = [r for r in rows if r['status'] != 'unattempted']
    known = [r for r in attempted if r.get('model_tokens') is not None]
    timed = [r for r in attempted if r.get('model_latency_ms') is not None]
    passed = sum(r['success'] for r in rows)
    checks = [c for r in rows for c in r['checks']]
    workflow_checks = {}
    for name, kind in (('corrections', None), ('restart', 'restart'),
                       ('recovery', 'resume'), ('repeat_confirmation', 'repeat'),
                       ('status', 'status'), ('provider_controls', 'provider')):
        selected = [c for c in checks if c.get('correction')] if kind is None else [
            c for c in checks if c['kind'] == kind]
        workflow_checks[name] = {'passed':sum(bool(c['passed']) for c in selected),
                                 'attempted':len(selected)}
    return {'scheduled':len(rows),'attempted':len(attempted),'passed':passed,
        'unattempted':len(rows)-len(attempted),
        'success_rate_scheduled':passed/len(rows) if rows else None,
        'success_rate_attempted':passed/len(attempted) if attempted else None,
        'provider_failures':sum(r.get('availability_failure',False) for r in rows),
        'budget_stops':sum(r.get('budget_stopped',False) for r in rows),
        'behavior_or_oracle_failures':sum(not r['success'] and r['status'] in ('failed','completed')
            and not r.get('availability_failure') and not r.get('budget_stopped') for r in rows),
        'interrupted':sum(r['status'] in ('running','interrupted') for r in rows),
        'wrong_payment_proposals':sum(c.get('wrong_payment_proposal',False) for r in rows for c in r['checks']),
        'unexpected_proposals':sum(c.get('unexpected_proposal',False) for r in rows for c in r['checks']),
        'duplicate_executions':sum(r.get('duplicate_executions',0) for r in rows),
        'unauthorized_effects':sum(r.get('unauthorized_effects',0) for r in rows),
        'workflow_checks':workflow_checks,
        'mean_model_tokens_known_trials':sum(r['model_tokens'] for r in known)/len(known) if known else None,
        'token_eligible_trials':len(known),
        'mean_model_latency_ms_timed_trials':sum(r['model_latency_ms'] for r in timed)/len(timed) if timed else None,
        'latency_eligible_trials':len(timed)}


def paired(rows):
    pairs = defaultdict(dict)
    for row in rows:
        pairs[(row['model'],row['scenario_id'])][row['context_mode']] = row
    counts = Counter()
    for pair in pairs.values():
        if set(pair) != {'history','structured'} or any(r['status']=='unattempted' for r in pair.values()):
            counts['missing_arm'] += 1
            continue
        h,s = pair['history'],pair['structured']
        counts['both_attempted'] += 1
        counts['both_pass' if h['success'] and s['success'] else 'both_fail' if not h['success'] and not s['success']
               else 'history_only_pass' if h['success'] else 'structured_only_pass'] += 1
        if any(r.get('availability_failure') or r.get('budget_stopped') or r['status'] in ('running','interrupted')
               for r in (h,s)):
            counts['pairs_with_infrastructure_or_budget_issue'] += 1
    return {key:counts[key] for key in ('both_attempted','missing_arm','both_pass','both_fail',
        'history_only_pass','structured_only_pass','pairs_with_infrastructure_or_budget_issue')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', type=Path, default=ROOT/'evals/conversations/protocols/expanded-freeze-v1.json')
    parser.add_argument('--output', type=Path, default=ROOT/'evals/conversations/results/expanded-results-v1.json')
    parser.add_argument('--review-notes', type=Path)
    args = parser.parse_args()
    freeze_path = args.freeze
    freeze = json.loads(freeze_path.read_text())
    all_rows, blocks = [], []
    for block in freeze['blocks']:
        candidates=[]
        for path in (ROOT/'instance/conversations').glob('*/report.json'):
            r=json.loads(path.read_text())
            if r.get('dataset_sha256')==block['dataset_sha256'] and r.get('code_sha256')==freeze['code_sha256']:
                candidates.append((path,r))
        if not candidates:
            raise ValueError('A frozen block has not been started.')
        owners={r['request_budget']['owner_run_id'] for _,r in candidates}
        if len(owners)!=1:
            raise ValueError('Multiple independent runs match the block; do not select a favorable run.')
        path,report=max(candidates,key=lambda item:item[1]['created_at'])
        if report['status']=='running':
            raise ValueError('Collection is still running; final export refused.')
        if report['model']!=block['model'] or len(report['trials'])!=block['scheduled_trials']:
            raise ValueError('Model or trial schedule differs from freeze.')
        expected={(case,mode) for case in block['case_ids'] for mode in freeze['modes']}
        if {(r['scenario_id'],r['context_mode']) for r in report['trials']}!=expected:
            raise ValueError('Trial labels differ from the frozen schedule.')
        rows=[]
        fields=('trial_id','scenario_id','family','context_mode','expected_outcome','status','success',
            'availability_failure','budget_stopped','model_calls','model_tokens','model_latency_ms',
            'refund_effects','successful_refunds','duplicate_executions','unauthorized_effects',
            'provider_post_attempts','preexisting_refunds','injected_external_refunds','unattempted_steps')
        allowed_checks=('step','kind','passed','turn_status','correction','proposed','payment_label_available',
                        'wrong_payment_proposal','unexpected_proposal','error','elapsed_ms',
                        'observed_stage','safe_nonexecution_checked','recovery_repeat_checked')
        for r in report['trials']:
            rows.append({**{k:r[k] for k in fields if k in r},'model':block['model'],
                'source_run_id':r.get('source_run_id',report['run_id']),
                'checks':[{k:v for k,v in c.items() if k in allowed_checks} for c in r['checks']],
                'call_statuses':[c['status'] for c in r['calls']]})
        usage=budget_observation(study_budget(path.parent.parent,report['request_budget']))
        blocks.append({'block':block['name'],'model':block['model'],'latest_run_id':report['run_id'],
            'collection_status':'all_trials_attempted' if all(r['status']!='unattempted' for r in rows) else 'stopped_with_untouched_trials',
            'source_run_ids':[r['run_id'] for _,r in sorted(candidates,key=lambda item:item[1]['created_at'])],
            'report_sha256':sha256(path.read_bytes()).hexdigest(),'status':report['status'],
            'budget_usage':usage,'by_mode':{m:counters([r for r in rows if r['context_mode']==m]) for m in freeze['modes']},
            'paired':paired(rows)})
        all_rows.extend(rows)
    by_family=defaultdict(list)
    for row in all_rows:
        by_family[row['family']].append(row)
    result={'study_id':freeze['study_id'],'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'collection_status':'all_trials_attempted' if all(r['status']!='unattempted' for r in all_rows) else 'stopped_with_untouched_trials',
        'freeze_sha256':sha256(freeze_path.read_bytes()).hexdigest(),'code_sha256':freeze['code_sha256'],
        'dataset_sha256':freeze['source_dataset_sha256'],'prompt_version':freeze['prompt_version'],
        'oracle_version':freeze.get('oracle_version','conversation-oracle-v1'),
        'evidence_type':'live_gemini_simulated_payments','stripe_calls':0,'unique_cases_scheduled':freeze['unique_cases'],
        'families_scheduled':freeze['families'],'scheduled_trials':freeze['scheduled_trials'],'blocks':blocks,
        'overall':counters(all_rows),'by_mode':{m:counters([r for r in all_rows if r['context_mode']==m]) for m in freeze['modes']},
        'paired':paired(all_rows),'by_family':{f:counters(rows) for f,rows in sorted(by_family.items())},
        'trials':all_rows,'analysis_tool_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'limitations':['Authored development coverage with overlap; no independent human label review.',
            'Different cases assigned to the models; do not rank models from these block scores.',
            'All-scheduled and attempted rates have different denominators; missingness and infrastructure failures are explicit.',
            'Token/latency means condition on available usage; latency includes pacing/network time.',
            'Workflow-check denominators include only reached checks; earlier failures can prevent later checks.',
            'Paired counts are descriptive for this dataset; no population independence or general memory advantage claim.']}
    notes=args.review_notes
    if notes is not None and notes.exists():
        result['qualitative_review']=json.loads(notes.read_text())
    output=args.output
    if output.exists():
        raise ValueError('Final export already exists; do not overwrite measured evidence.')
    output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'path':str(output),'overall':result['overall'],'paired':result['paired']},indent=2))


if __name__=='__main__':
    try:
        main()
    except (ValueError,OSError) as error:
        raise SystemExit(f'Export stopped: {error}')
