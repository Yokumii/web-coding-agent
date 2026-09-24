import asyncio
import hashlib
import json

import pytest

from src.orchestration import fast_edit_gt as fast
from src.orchestration import edit_skills
from src.orchestration.skill_rsi import check_helpers,enqueue,helper_context,validate_helper


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
    h=helper(); h['code']+=' '*1800
    with pytest.raises(ValueError): validate_helper(h)


def test_queue_is_bounded_and_keeps_evidence(tmp_path):
    path=enqueue(output=tmp_path,before=[],after=[{'path':'a.js','code':'x'*30000}],
        skill='webcompass-page-transitions',item={'task_type':'Page Transitions'},evidence_ref='checkpoint.json')
    packet=json.loads(open(path).read())
    assert packet['source'][0]['truncated']
    assert len(packet['source'][0]['code'])<=12000
    assert packet['evidence_ref']=='checkpoint.json'


def test_pinned_reference_and_document_use_same_folder(tmp_path,monkeypatch):
    name='webcompass-page-transitions'
    snapshot=tmp_path/'.harness/skill_versions'/name/'v1'
    (snapshot/'references').mkdir(parents=True)
    (snapshot/'references/transitions.mjs').write_text('new implementation')
    (tmp_path/'.harness/skill_version_lock.json').write_text(json.dumps({'skills':{name:{'version':'v1'}}}))
    monkeypatch.setattr(edit_skills,'read_edit_task_contract',lambda _: {'chain_metadata':{
        'task_type':'Page Transitions','edit_skill_stack':'react'}})
    assert edit_skills.selected_reference_files(tmp_path)==[snapshot/'references/transitions.mjs']
