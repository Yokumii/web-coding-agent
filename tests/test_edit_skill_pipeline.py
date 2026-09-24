"""Input, selected-stack staging and real transactional component reuse."""
import hashlib
import json
from pathlib import Path
import subprocess
import pytest
from src.config import HarnessConfig
from src.agents.generator import _run_atomic_patch_executor, AtomicCandidateRejected
from src.orchestration.edit_skills import (EDIT_SKILLS, render_edit_skill, selected_reference_files,
    staged_reference_revisions, reference_destinations, acceptance_recipe)
from src.orchestration.minimal_path_guidance import MinimalPathPolicy
from src.orchestration.file_comm import FileComm
from scripts.run_g2_compound_edit import normalize_input_case

@pytest.fixture
def anyio_backend(): return 'asyncio'

def contract(root, task_type='Shopping Cart'):
    folder=root/'.harness';folder.mkdir(exist_ok=True)
    (folder/'edit_task_contract.json').write_text(json.dumps({'schema_version':'edit-task-contract-v1','chain_metadata':{'task_type':task_type}}))

def test_0921_input_preserves_code_and_order_and_rejects_pending():
    row={'instance_id':'input','task_type':['Data Table'], 'instruction':{
        'src_code':[{'path':'index.html','code':'<main>Original</main>'}],
        'description':[{'task_type':'Data Table','description':'Sort these records.'}]},
        'metadata':{'instruction_status':'query_ready'}}
    normalized=normalize_input_case(row)
    assert normalized['source_code']==row['instruction']['src_code']
    assert normalized['descriptions']==row['instruction']['description']
    row['metadata']['instruction_status']='awaiting_query'
    with pytest.raises(ValueError,match='query_ready'):normalize_input_case(row)
    row['metadata']['instruction_status']='query_ready';row['task_type']=['Shopping Cart']
    with pytest.raises(ValueError,match='disagree'):normalize_input_case(row)

@pytest.mark.parametrize('stack',['vanilla','react','vue'])
def test_selected_stack_only_and_tamper_detection(tmp_path,stack):
    frontend=tmp_path/'frontend';frontend.mkdir()
    if stack!='vanilla':(frontend/'package.json').write_text(json.dumps({'dependencies':{stack:'*'}}))
    for task_type in EDIT_SKILLS:
        contract(tmp_path,task_type)
        render_edit_skill(tmp_path)
        files=selected_reference_files(tmp_path)
        names=[p.name for p in files]
        assert ('Component.jsx' in names)==(stack=='react')
        assert ('Component.vue' in names)==(stack=='vue')
        assert bool([p for p in files if p.suffix=='.mjs'])==(stack!='vanilla')
        assert acceptance_recipe(task_type)['flow']
        revisions=staged_reference_revisions(tmp_path)
        assert len(revisions)==len(files)
    (tmp_path/next(iter(revisions))).write_text('tampered')
    with pytest.raises(ValueError,match='changed'):staged_reference_revisions(tmp_path)

@pytest.mark.anyio
@pytest.mark.parametrize('invalid_destination',[False,True,'omitted','adapt'])
async def test_atomic_reference_copy_preserves_scope_and_does_not_echo_code(tmp_path,invalid_destination):
    frontend=tmp_path/'frontend';frontend.mkdir();contract(tmp_path)
    html='<main>Existing source</main>\n';(frontend/'index.html').write_text(html)
    def git(*args):return subprocess.check_output(['git',*args],cwd=frontend,text=True).strip()
    git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.com')
    git('add','.');git('commit','-qm','baseline');baseline=git('rev-parse','HEAD')
    fc=FileComm(tmp_path/'.harness');fc.write_state({'supplied_atomic_plan':True})
    plan={'schema_version':'minimal-path-plan-v3','round':1,'source_change_cone':{
        'initial_paths':['frontend/index.html'],'local_paths':['frontend/index.html'],
        'dependency_paths':[],'planned_new_paths':[]},'route_scope':{},
        'budgets':{'max_patch_lines':10,'max_touched_files':4}}
    (fc.dir/'minimal_path_plan_round_1.json').write_text(json.dumps(plan))
    (fc.dir/'edit_context_round_1.json').write_text(json.dumps({'schema_version':'edit-context-v1','source_windows':[
        {'path':'frontend/index.html','content':html,'file_sha256':hashlib.sha256(html.encode()).hexdigest()}]}))
    render_edit_skill(tmp_path);mapping=reference_destinations(tmp_path)
    source=next(x for x in mapping if x.endswith('.js'));destination=mapping[source]
    if invalid_destination is True:destination='frontend/unrelated.js'
    candidate=json.dumps({'operations':[{'op':'copy_from','source':source,'path':destination}]})
    if invalid_destination is False:
        candidate=json.dumps({'operations':[{'op':'copy_from','source':source,'path':destination}],
            'patches':[{'path':'frontend/index.html','old_text':html,'new_text':html}]})
    if invalid_destination == 'adapt':
        candidate=json.dumps({'operations':[{'op':'copy_from','source':source,'path':destination}],
            'patches':[{'path':destination,'old_text':'Your basket is empty.','new_text':'Your reading basket is empty.'}]})
    if invalid_destination == 'omitted':
        candidate=json.dumps({'operations':[{'op':'insert_after','path':'frontend/index.html','after_line':1,'content':'<p>Rewritten feature</p>'}]})
    policy=MinimalPathPolicy.load(tmp_path,1)
    config=HarnessConfig(agent_runtime='openai',edit_skills_enabled=True,edit_frozen_compound_mode=True,minimality_guard_enabled=False)
    kwargs=dict(config=config,file_comm=fc,workdir=tmp_path,round_num=1,mode='generate',prompt='',baseline_commit=baseline,mutation_policy=policy,candidate_override=candidate)
    if invalid_destination is True or invalid_destination == 'omitted':
        with pytest.raises(AtomicCandidateRejected,match='must be reused' if invalid_destination == 'omitted' else 'component directory'):await _run_atomic_patch_executor(**kwargs)
        assert git('rev-parse','HEAD')==baseline
        assert not (tmp_path/destination).exists()
        assert policy.initial_paths=={'frontend/index.html'}
    else:
        await _run_atomic_patch_executor(**kwargs)
        expected=(tmp_path/source).read_text()
        if invalid_destination == 'adapt':expected=expected.replace('Your basket is empty.','Your reading basket is empty.')
        assert (tmp_path/destination).read_text()==expected
        assert len((tmp_path/destination).read_text().splitlines())>10
        assert (frontend/'index.html').read_text()==html
        assert git('rev-parse','HEAD')!=baseline
        # A subsequent repair can request the same library without erasing
        # already integrated local adaptations or failing on an existing file.
        component=tmp_path/destination
        adapted=component.read_text()+'\n// Local integration retained.\n'
        component.write_text(adapted)
        git('add','.');git('commit','-qm','local adaptation')
        kwargs.update(baseline_commit=git('rev-parse','HEAD'),
            mutation_policy=MinimalPathPolicy.load(tmp_path,1),
            candidate_override=json.dumps({'operations':[{'op':'copy_from','source':source,'path':destination}],
                'patches':[{'path':'frontend/index.html','old_text':html,'new_text':html+'<p>Integrated</p>\n'}]}))
        await _run_atomic_patch_executor(**kwargs)
        assert component.read_text()==adapted


def test_vanilla_reference_wiring_is_scoped_before_host_and_idempotent(tmp_path):
    from src.orchestration.edit_skills import vanilla_reference_wiring
    frontend=tmp_path/'frontend';frontend.mkdir();contract(tmp_path,'Drag & Drop Interface')
    html='<html><head><link rel="stylesheet" href="styles.css"></head><body><script src="game.js"></script></body></html>'
    page=frontend/'index.html';page.write_text(html);other=frontend/'other.html';other.write_text(html)
    render_edit_skill(tmp_path)
    for source,destination in reference_destinations(tmp_path).items():
        target=tmp_path/destination;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((tmp_path/source).read_bytes())
    patches=vanilla_reference_wiring(tmp_path,{'frontend/index.html'})
    assert len(patches)==2
    for patch in patches:page.write_text(page.read_text().replace(patch['old_text'],patch['new_text'],1))
    text=page.read_text()
    assert text.index('drag-drop.js')<text.index('game.js')
    assert text.index('drag-drop.css')<text.index('styles.css')
    assert other.read_text()==html
    assert vanilla_reference_wiring(tmp_path,{'frontend/index.html'})==[]
