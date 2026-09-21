from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.export_session_six_tasks import (
    TASKS, build_row, compound_edits, export_six_tasks, sampling_manifest,
)
from scripts.export_trajectory_dataset import apply_patches, make_patches, _repair_task_descriptions
from src.orchestration.webcompass_protocol import EDIT_TYPES


def chain(n):
    result=[]
    source=[{'path':'index.html','code':'<main>Seed</main>'}]
    types=sorted(EDIT_TYPES)
    for i in range(1,n+1):
        target=[{'path':'index.html','code':source[0]['code']+f'<section>Feature {i}</section>'}]
        result.append({'instance_id':f'e{i}','source_project':'/unused', 'task':'text-editing','task_type':[types[i-1]],
            'description':f'Add feature {i}', 'instruction':{'src_code':source,'description':f'Add feature {i}'},
            'reference':{'dst_code':target},'label_modified_files':make_patches(source,target,types[i-1]),
            'trajectory':{'source_commit':str(i-1),'destination_commit':str(i),'edit_id':f'q{i}','session_id':'lineage'},
            'quality':{'edit_kind':'atomic_edit','task_descriptions':[{'task_type':types[i-1],'description':f'Add feature {i}'}]}})
        source=target
    return result


def test_windows_preserve_dependencies_and_replay():
    records=chain(8)
    windows=compound_edits(records)
    assert len(windows)==15
    assert {r['quality']['task_count'] for r in windows}==set(range(4,9))
    for r in windows:
        assert apply_patches(r['instruction']['src_code'],r['label_modified_files'])==r['reference']['dst_code']
        assert len(r['trajectory']['included_edit_ids'])==len(r['task_type'])
    broken=copy.deepcopy(records);broken[3]['instruction']['src_code']=[]
    with pytest.raises(ValueError,match='non-contiguous'):
        compound_edits(broken)


def test_six_model_inputs_keep_repair_answers_hidden_and_images_ordered(tmp_path):
    record=chain(1)[0]
    def snapshot(role):return {'images':[{'path':role+'.png','route':'/','state':'initial'}],
                               'pages':[{'path':'index.html','route':'/'}],'resources':[]}
    source,target=snapshot('before'),snapshot('after')
    for task in TASKS:
        row=copy.deepcopy(record)
        if task.endswith('generation'):
            row['instruction']['src_code']=[]
        if task.endswith('repair'):
            row['quality']['repair_task_descriptions']=[{'task_type':'Missing Attributes','description':'PRIVATE_LABEL_LOCATION'}]*3
        result=build_row(row,task,source if not task.endswith('generation') else None,target,tmp_path)
        prompt=result['messages'][0]['content']
        assert result['images']==result['input_images']
        if task.startswith('text-'):assert result['images']==[]
        if task=='image-generation':
            assert result['images']==['after.png']
            assert 'Add feature 1' not in prompt
            assert '<main>Seed</main>' not in prompt
        if task=='image-editing':assert result['images']==['before.png']
        if task=='image-repair':assert result['images']==['before.png','after.png']
        if task.endswith('repair'):
            assert 'only 3 issues' in prompt
            assert 'PRIVATE_LABEL_LOCATION' not in prompt
            assert '<search_replace' in result['messages'][1]['content']


def test_same_type_problems_are_not_collapsed_or_counted_twice():
    evidence={'checks':[{'check_id':'risk','steps':[{'action':'assert_webcompass_risk','ok':False,'output':{'actual':{
        'defect_type':'Missing Attributes','passed':False,'issues':[
            {'element':'#a','element_path':'#a','kind':'control-missing-accessible-name'},
            {'element':'#b','element_path':'#b','kind':'control-missing-accessible-name'}]}}}]}]}
    descriptions=[{'task_type':'Missing Attributes','description':'Missing label','evidence_ids':['risk'],
                   'issue_locations':[{'element_path':sel,'kind':'control-missing-accessible-name'}]} for sel in ['#a','#b','#a']]
    grade={'hidden_oracle':{'status':'failed','failed_check_ids':['risk']},'repair_task_descriptions':descriptions}
    result=_repair_task_descriptions(grade,hidden_evidence=evidence)
    assert len(result)==2
    assert '#b' not in result[0]['description']
    grade['repair_task_descriptions'] = [{**descriptions[0], 'issue_locations': descriptions[0]['issue_locations'] + descriptions[1]['issue_locations']}]
    assert len(_repair_task_descriptions(grade, hidden_evidence=evidence)) == 2


def test_distribution_weights_preserve_all_and_mark_gaps():
    distribution={'counts':{family:{mode:{str(n):1 for n in range(4,13)} for mode in ['sp','mp']} for family in ['editing','repair']}}
    rows={t:[] for t in TASKS}
    rows['text-editing']=[{'instance_id':str(i),'metadata':{'task_count':4,'count_aligned':True,'page_mode':'sp'}} for i in range(3)]
    report=sampling_manifest(rows,distribution)
    stratum=report['strata']['text-editing/sp']
    assert len(stratum['samples'])==3
    assert sum(s['weight'] for s in stratum['samples'])==pytest.approx(1/18)
    assert stratum['missing_counts']==list(range(5,13))


@pytest.mark.anyio
async def test_export_screenshots_match_versions_and_resume_without_rendering(tmp_path,monkeypatch):
    from scripts import export_session_six_tasks as exporter
    frontend=tmp_path/'run/frontend';frontend.mkdir(parents=True)
    def git(*args):return subprocess.check_output(['git',*args],cwd=frontend,text=True).strip()
    git('init','-q');git('config','user.name','Test');git('config','user.email','test@localhost')
    (frontend/'index.html').write_text('<main>Before</main>')
    (frontend/'other.html').write_text('<main style="height:2000px">Second page</main>')
    (frontend/'asset.bin').write_bytes(b'resource')
    git('add','.');git('-c','commit.gpgsign=false','commit','-qm','seed');before=git('rev-parse','HEAD')
    code1=[{'path':p.name,'code':p.read_text()} for p in sorted(frontend.glob('*.html'))]
    (frontend/'index.html').write_text('<main>After</main>');git('add','.');git('-c','commit.gpgsign=false','commit','-qm','edit');after=git('rev-parse','HEAD')
    code2=[{'path':p.name,'code':p.read_text()} for p in sorted(frontend.glob('*.html'))]
    row=chain(1)[0];row.update(source_project=str(frontend),instruction={'src_code':code1},reference={'dst_code':code2},label_modified_files=make_patches(code1,code2,'Data Table'))
    row['trajectory'].update(source_commit=before,destination_commit=after)
    gen=copy.deepcopy(row);gen.update(instance_id='gen',task='text-generation');gen['instruction']={'src_code':[]}
    distribution=Path(__file__).parents[1]/'src/orchestration/webcompass_subtask_distribution.json'
    output=tmp_path/'export'
    first=await export_six_tasks([row,gen],{'session_id':'lineage'},output,distribution)
    assert first['counts']['image-generation']==first['counts']['image-editing']==1
    path=output/first['records']['image-editing']['path'];record=json.loads(path.read_text())
    assert len(record['images'])==2
    from PIL import Image
    with Image.open(output/record['images'][1]) as screenshot:
        assert screenshot.height >= 2000
    assert len(record['metadata']['page_manifest']['source'])==2
    assert record['metadata']['resources'][0]['path']=='asset.bin'
    real_browser = exporter.collect_browser_evidence
    async def no_browser(**kwargs):raise AssertionError('resume must use version cache')
    monkeypatch.setattr(exporter,'collect_browser_evidence',no_browser)
    second=await export_six_tasks([row,gen],{'session_id':'lineage'},output,distribution)
    assert first==second
    assert path.read_text().count('"schema_version"')==1
    monkeypatch.setattr(exporter,'collect_browser_evidence',real_browser)
    asset = output/record['metadata']['resources'][0]['asset']
    asset.write_bytes(b'corrupt cache')
    await export_six_tasks([row,gen],{'session_id':'lineage'},output,distribution)
    assert asset.read_bytes() == b'resource'


def test_batch_weights_are_recomputed_after_pooling(tmp_path):
    from scripts.export_session_six_tasks import merge_batch_indexes, atomic_json
    outputs=[]
    for i in range(2):
        root=tmp_path/str(i);outputs.append(root)
        atomic_json(root/'dataset_index.json',{'session_id':str(i),'sampling_manifest':'sampling.json','counts':{task:1 for task in TASKS}})
        atomic_json(root/'sampling.json',{'strata':{'text-editing/sp':{
            'target_probabilities':{'4':0.5,'5':0.5},'samples':[{'instance_id':str(i),'task_count':4,'weight':0.5}]}}})
    result=merge_batch_indexes(outputs,tmp_path/'index.json')
    assert result['counts']['text-generation']==2
    assert [s['weight'] for s in result['strata']['text-editing/sp']['samples']]==[0.125,0.125]
    assert result['strata']['text-editing/sp']['missing_counts']==[5]
