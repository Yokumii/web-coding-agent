#!/usr/bin/env python3
"""Deterministic coverage sample from a release shard; never modifies the release."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path


def features(row):
    tasks=row['instruction']['description']
    deps={}
    for file in row['instruction']['src_code']:
        if Path(file['path']).name=='package.json':
            try:
                package=json.loads(file['code'])
                deps.update(package.get('dependencies') or {})
                deps.update(package.get('devDependencies') or {})
            except (ValueError,TypeError): pass
    paths=[f['path'] for f in row['instruction']['src_code']]
    framework=('react' if 'react' in deps or any(p.endswith(('.jsx','.tsx')) for p in paths)
               else 'vue' if 'vue' in deps or any(p.endswith('.vue') for p in paths) else 'vanilla')
    page=row.get('page_type','unknown')
    types=[t['task_type'] for t in tasks]
    size=sum(len(f['code']) for f in row['instruction']['src_code'])
    return {f'type:{t}' for t in types} | {
        f'first:{types[0]}', f'framework:{framework}', f'page:{page}',
        f'stratum:{page}/{framework}', f'length:{len(tasks)}',
        f'size:{min(size//30000,4)}'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--shard',type=Path,required=True)
    p.add_argument('--sha256',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--include-row',type=int,help='Include an already validated release row first')
    p.add_argument('--unchanged-validated-mothers',type=Path,
                   help='Sample only mothers accepted without source repair, from this same release snapshot')
    a=p.parse_args()
    digest=hashlib.sha256(a.shard.read_bytes()).hexdigest()
    if digest!=a.sha256: raise ValueError('source shard hash mismatch')
    with gzip.open(a.shard,'rt') as f: rows=[json.loads(line) for line in f]
    population=len(rows)
    eligible=set(range(population))
    if a.unchanged_validated_mothers:
        root=a.unchanged_validated_mothers
        mothers=json.loads((root/'plan.json').read_text())
        if mothers['source_sha256']!=digest:
            raise ValueError('mother validation belongs to a different release snapshot')
        accepted=set()
        for job in mothers['jobs']:
            result=root/'results'/(job['project_id']+'.json')
            if result.is_file() and json.loads(result.read_text()).get('status')=='accepted_without_repair':
                accepted.update(job['references'])
        eligible={i for i,row in enumerate(rows) if row['instance_id'] in accepted}
        if a.limit>len(eligible): raise ValueError('not enough validated unchanged mothers')
    if not 1<=a.limit<=len(rows): raise ValueError('invalid sample size')
    tags=[features(r) for r in rows]
    seen=Counter(); selected=[]; remaining=set(eligible)
    if a.include_row is not None:
        if a.include_row not in remaining: raise ValueError('invalid included row')
        selected.append(a.include_row); remaining.remove(a.include_row); seen.update(tags[a.include_row])
    # Rare first-step types matter: later steps may not execute after a failed prefix.
    weights={'first':4,'type':2,'stratum':3,'framework':1,'page':4,'length':2,'size':1}
    while len(selected)<a.limit:
        i=max(sorted(remaining),key=lambda i:sum(weights[t.split(':')[0]]/(1+seen[t])**2 for t in tags[i]))
        selected.append(i); remaining.remove(i); seen.update(tags[i])
    a.output.mkdir(parents=True,exist_ok=False)
    jobs=[]
    for ordinal,i in enumerate(selected):
        row=rows[i]; ident=f'case_{ordinal+1:03d}'
        path=(a.output/(ident+'.json')).resolve()
        path.write_text(json.dumps(row,ensure_ascii=False))
        jobs.append({'instance_id':ident,'source_instance_id':row['instance_id'],
            'source_row':i,'case':str(path),'task_count':len(row['instruction']['description'])})
    plan={'source':str(a.shard),'source_sha256':digest,'population':population,'eligible_population':len(eligible),
        'unchanged_validated_mothers':str(a.unchanged_validated_mothers) if a.unchanged_validated_mothers else None,
        'selected':len(jobs),'coverage':dict(sorted(seen.items())),
        'uncovered_features':sorted(set.union(*tags)-seen.keys()),'jobs':jobs}
    (a.output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in plan.items() if k!='jobs'},ensure_ascii=False))


if __name__=='__main__': main()
