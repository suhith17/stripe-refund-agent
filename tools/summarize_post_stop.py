"""Export a post-stop continuation while proving earlier attempted rows unchanged."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.summarize_expanded_study import counters


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol',type=Path,default=ROOT/'evals/conversations/protocols/expanded-continuation-v3.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    protocol=json.loads(args.protocol.read_text())
    baseline_path=ROOT/protocol['original_export']
    if sha256(baseline_path.read_bytes()).hexdigest()!=protocol['original_export_sha256']:
        raise ValueError('Original stop snapshot changed.')
    if args.output.exists():
        raise ValueError('Output already exists; never overwrite measured evidence.')
    baseline=json.loads(baseline_path.read_text())
    freeze=json.loads((ROOT/protocol['original_freeze']).read_text())
    analysis=ROOT/'tools/summarize_expanded_study.py'
    if sha256(analysis.read_bytes()).hexdigest()!=freeze['files']['tools/summarize_expanded_study.py']:
        raise ValueError('Frozen analysis tool changed.')
    scratch=ROOT/'instance/expanded-study-v3'/f'export-{uuid4().hex}.json'
    scratch.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,str(analysis),'--freeze',str(ROOT/protocol['original_freeze']),
                    '--output',str(scratch),
                    '--review-notes',str(ROOT/'instance/expanded-study-v2/review-notes.json')],
                   cwd=ROOT,check=True,capture_output=True)
    result=json.loads(scratch.read_text())
    old={(r['model'],r['trial_id']):r for r in baseline['trials']}
    new={(r['model'],r['trial_id']):r for r in result['trials']}
    if old.keys()!=new.keys() or result['code_sha256']!=baseline['code_sha256'] or result['dataset_sha256']!=baseline['dataset_sha256']:
        raise ValueError('Continuation changed the schedule, source or labels.')
    added=[]
    for key,previous in old.items():
        current=new[key]
        if previous['status']!='unattempted' and current!=previous:
            raise ValueError('A previously attempted result changed.')
        if previous['status']=='unattempted' and current['status']!='unattempted':
            added.append(current)
    result['collection_phase']='post_stop_continuation_under_changed_billing_conditions'
    result['post_stop']={
        'protocol':str(args.protocol.relative_to(ROOT) if args.protocol.is_absolute() else args.protocol),
        'protocol_sha256':sha256(args.protocol.read_bytes()).hexdigest(),
        'baseline_export':protocol['original_export'],
        'baseline_sha256':protocol['original_export_sha256'],
        'baseline_overall':baseline['overall'],
        'billing_change':protocol.get('billing_change'),
        'newly_attempted':counters(added),
        'new_trial_ids':[{'model':r['model'],'trial_id':r['trial_id']} for r in added],
        'prior_attempted_rows_preserved':True,
        'postprocessor_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'interpretation':'Cumulative coverage includes a post-stop continuation with changed billing conditions. Preserve and report the original stop snapshot separately.'}
    result['limitations'].append(result['post_stop']['interpretation'])
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(result,handle,indent=2)
        handle.write('\n')
    print(json.dumps({'path':str(args.output),'overall':result['overall'],
                      'newly_attempted':result['post_stop']['newly_attempted']},indent=2))


if __name__=='__main__':
    main()
