import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.orchestration import fast_edit_gt as fast
from src.orchestration import edit_skills
from src.orchestration.skill_rsi import check_helpers,enqueue,helper_context,validate_helper,learn,normalize_test_json


def helper():
    return {'name':'clampValue','code':'function clampValue(x, min, max) { return Math.max(min, Math.min(max, x)); }',
        'usage':'clampValue(x, min, max): clamp a numeric input.',
        'tests':[{'args_json':'[-1,0,10]','expected_json':'0'}, {'args_json':'[20,0,10]','expected_json':'10'}]}


def test_helper_browser_execution_and_wrong_result_rejected():
    h=helper()
    asyncio.run(check_helpers([h]))
    h['tests'][0]['expected_json']='99'
    with pytest.raises(ValueError,match='compatibility'):
        asyncio.run(check_helpers([h]))


def test_catalog_is_loaded_as_direct_code_and_short_instructions(tmp_path,monkeypatch):
    name='webcompass-page-transitions'
    folder=tmp_path/name
    (folder/'references').mkdir(parents=True)
    (folder/'SKILL.md').write_text('Skill instructions')
    helpers=[helper()]
    revision=hashlib.sha256(json.dumps(helpers,sort_keys=True).encode()).hexdigest()
    (folder/'references/learned.json').write_text(json.dumps({'helpers':helpers,'revision':revision}))
    monkeypatch.setattr(fast,'SKILLS_ROOT',tmp_path)
    assert 'clampValue' in fast.skill_instructions('Page Transitions')['instructions']
    refs=fast.references([{'path':'package.json','code':'{"dependencies":{"react":"*"}}'}],'Page Transitions')
    assert refs['components/edit-skills/webcompass-page-transitions/learned.mjs'].startswith(helpers[0]['code'])
    (folder/'references/learned.json').write_text('broken')
    assert helper_context(folder)==('',{})


def test_oversized_and_impure_helpers_rejected():
    h=helper(); h['code']='function clampValue(x) { return window.location; }'
    with pytest.raises(ValueError): validate_helper(h)


def test_plain_string_expected_value_is_normalized_to_json():
    h=helper()
    h['tests'][0]['expected_json']='1 Hour'
    normalize_test_json(h)
    assert json.loads(h['tests'][0]['expected_json'])=='1 Hour'
    validate_helper(h)
    h=helper(); h['code']+=' '*1800
    with pytest.raises(ValueError): validate_helper(h)


def test_queue_is_bounded_and_keeps_evidence(tmp_path):
    path=enqueue(output=tmp_path,before=[],after=[{'path':'a.js','code':'x'*30000}],
        skill='webcompass-page-transitions',item={'task_type':'Page Transitions'},evidence_ref='checkpoint.json')
    packet=json.loads(open(path).read())
    assert packet['source'][0]['truncated']
    assert len(packet['source'][0]['code'])<=12000
    assert packet['evidence_ref']=='checkpoint.json'


def test_queue_evidence_survives_case_relocation(tmp_path):
    path=enqueue(output=tmp_path,before=[],after=[{'path':'adapter.js','code':'function identity(x) { return x; }'}],
        skill='webcompass-tree-view',item={'task_type':'Tree View'},evidence_ref=tmp_path/'step_01/checkpoint.json')
    assert json.loads(open(path).read())['evidence_ref']=='step_01/checkpoint.json'


def test_pinned_reference_and_document_use_same_folder(tmp_path,monkeypatch):
    name='webcompass-page-transitions'
    snapshot=tmp_path/'.harness/skill_versions'/name/'v1'
    (snapshot/'references').mkdir(parents=True)
    (snapshot/'references/transitions.mjs').write_text('new implementation')
    (tmp_path/'.harness/skill_version_lock.json').write_text(json.dumps({'skills':{name:{'version':'v1'}}}))
    monkeypatch.setattr(edit_skills,'read_edit_task_contract',lambda _: {'chain_metadata':{
        'task_type':'Page Transitions','edit_skill_stack':'react'}})
    assert edit_skills.selected_reference_files(tmp_path)==[snapshot/'references/transitions.mjs']


def test_learner_failure_does_not_change_completed_batch(tmp_path,monkeypatch):
    from scripts import run_g2_compound_batch as batch
    plan=tmp_path/'plan.json'
    plan.write_text(json.dumps({'jobs':[{'instance_id':'ok','case':'unused','task_count':1}]}))
    args=batch.parser().parse_args(['--plan',str(plan),'--output',str(tmp_path/'batch'),'--workers','1'])
    monkeypatch.setattr(batch,'_run_one',lambda *args:{'status':'ok','instance_id':'ok'})
    def broken(*_):
        raise ValueError('broken RSI result')
    monkeypatch.setattr(batch,'_post_batch_rsi',broken)
    result=batch.run(args)
    assert result['status']=='complete' and result['counts']=={'ok':1}
    assert result['rsi']['status']=='skipped'
    assert json.loads((args.output/'status.json').read_text())['rsi']['status']=='skipped'


def test_rsi_luna_uses_degraded_credential_and_responses(tmp_path,monkeypatch):
    from pathlib import Path
    from argparse import Namespace
    from scripts import run_skill_rsi as runner
    config=tmp_path/'.config/webcoding'
    (config/'credentials').mkdir(parents=True)
    (config/'credentials/njulink-degraded.key').write_text('test-degraded')
    (config/'experimental-luna.json').write_text(json.dumps({
        'base_url':'https://example.invalid','http_headers':{'test-header':'test-value'},
        'bearer_token':'must-not-use'}))
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    library=tmp_path/'library';library.mkdir()
    async def learn(packet,lib,cfg,output):
        assert cfg.openai_api_key=='test-degraded'
        assert cfg.openai_wire_api=='responses'
        assert cfg.openai_extra_headers=={'test-header':'test-value'}
        assert cfg.generator_model=='gpt-5.6-luna'
        return {'status':'skipped','reason':'test'}
    monkeypatch.setattr(runner,'learn',learn)
    assert runner.run(Namespace(output=tmp_path/'out',library=library,packet=None,
        provider_profile='experimental-luna',model=None))['status']=='skipped'


def test_rsi_selects_one_packet_per_skill_with_a_small_batch_cap(tmp_path):
    from scripts.run_g2_compound_batch import _select_rsi_packets
    packets=[]
    for index,skill in enumerate(('one','one','two','three')):
        path=tmp_path/f'{index}.json';path.write_text(json.dumps({'skill':skill}));packets.append(path)
    selected=_select_rsi_packets(packets)
    assert list(selected)==['one','two']


def test_rsi_publishes_a_versioned_skill_reference(tmp_path,monkeypatch):
    skill='webcompass-tree-view';folder=tmp_path/'skills'/skill
    (folder/'references').mkdir(parents=True);(folder/'SKILL.md').write_text('Tree instructions')
    checkpoint=tmp_path/'case/step_01/checkpoint.json';checkpoint.parent.mkdir(parents=True)
    source={'path':'adapter.js','code':'function clampValue(x, min, max) { return Math.max(min, Math.min(max, x)); }'}
    checkpoint.write_text(json.dumps({'browser_verified':True,'target':[source]}))
    packet=tmp_path/'case/rsi_queue/packet.json';packet.parent.mkdir()
    packet.write_text(json.dumps({'skill':skill,'instruction':{'task_type':'Tree View'},
        'source':[source],'evidence_ref':'step_01/checkpoint.json'}))
    proposal={'action':'upsert','reason':'reusable','name':'clampValue','code':source['code'],
        'usage':'Clamp a numeric value to inclusive bounds.','tests':[
          {'args_json':'[-1,0,10]','expected_json':'0'}, {'args_json':'[20,0,10]','expected_json':'10'}]}
    class Client:
        def __init__(self,*_): pass
        async def complete(self,**_):
            return {'usage':{'prompt_tokens':1,'completion_tokens':1},
                'choices':[{'message':{'content':json.dumps(proposal)}}]}
    import src.agents.openai_runner as runner
    import src.orchestration.skill_rsi as rsi
    monkeypatch.setattr(runner,'OpenAIHTTPClient',Client)
    monkeypatch.setattr(rsi,'EDIT_SKILLS',{'Tree View':skill},raising=False)
    config=SimpleNamespace(generator_model='test',openai_wire_api='responses')
    result=asyncio.run(learn(packet,tmp_path/'skills',config,tmp_path/'out'))
    catalog=json.loads((folder/'references/learned.json').read_text())
    assert result['status']=='published' and result['version']==1
    assert catalog['version']==1 and catalog['helpers'][0]['name']=='clampValue'
