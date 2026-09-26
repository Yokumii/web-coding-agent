#!/usr/bin/env python3
"""Summarize real validation artifacts, including failed and diagnostic request usage."""
import argparse
from collections import Counter
import json
from pathlib import Path


def report(root):
    sample=root/'sample_v2' if (root/'sample_v2').exists() else root/'sample'
    plan=json.loads((sample/'plan.json').read_text())
    states=Counter(); attempted=Counter(); accepted=Counter(); failures=[]; seconds=[]; repairs=0
    for job in plan['jobs']:
        folder=root/'batch'/job['instance_id']
        result=folder/'result.json'
        if not result.exists():
            states['running' if folder.exists() else 'pending']+=1
        else:
            value=json.loads(result.read_text()); states[value['status']]+=1
            if value['status']!='ok':
                failures.append({'case':job['instance_id'],'source':job['source_instance_id'],
                    'completed_steps':value.get('completed_steps',0),'error':value.get('error'),
                    'failed_steps':[s for s in value.get('steps',[]) if s['status']!='ok']})
        for step in folder.glob('step_*'):
            if not step.is_dir(): continue
            index=int(step.name.split('_')[1])-1
            row=json.loads(Path(job['case']).read_text())
            task_type=row['instruction']['description'][index]['task_type']
            attempted[task_type]+=1
            if (step/'result.json').exists():
                value=json.loads((step/'result.json').read_text())
                if value['status']=='ok': accepted[task_type]+=1
                seconds.append(value.get('elapsed_seconds',0))
            repairs+=sum(p.is_dir() for p in step.glob('repair_*'))
    usage=Counter(); calls=[]
    for scope in [root/'batch',root/'rsi_smoke',*root.glob('smoke_attempt_*')]:
        for path in scope.glob('**/call_*.json'):
            metric=json.loads(path.read_text()); tokens=metric.get('usage') or {}
            usage['calls']+=1
            usage['input_tokens']+=tokens.get('prompt_tokens',tokens.get('input_tokens',0))
            usage['output_tokens']+=tokens.get('completion_tokens',tokens.get('output_tokens',0))
            usage['unknown_usage_calls']+=not bool(tokens)
            calls.append({'path':str(path.relative_to(root)),'status':metric.get('status'),
                'seconds':metric.get('elapsed_seconds'),'error':metric.get('error')})
    return {'selected':plan['selected'],'states':dict(states),'planned_coverage':plan['coverage'],
        'attempted_types':dict(attempted),'accepted_types':dict(accepted),'repair_rounds':repairs,
        'subtask_seconds':{'count':len(seconds),'max':max(seconds,default=0),
            'mean':round(sum(seconds)/len(seconds),3) if seconds else None},
        'usage_including_smoke_and_rsi':dict(usage),'billed_cost':None,
        'cost_note':'Provider has not returned a verified monetary amount; unknown requests may be billed.',
        'failures':failures,'calls':calls}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('root',type=Path)
    p.add_argument('--output',type=Path)
    a=p.parse_args(); value=report(a.root)
    if a.output: a.output.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in value.items() if k not in {'planned_coverage','failures','calls'}},ensure_ascii=False))
