import pytest
from src.orchestration.fast_edit_gt import apply_candidate, references, lenient_check, scoped_payload
from scripts.export_trajectory_dataset import apply_patches
from src.orchestration.fast_edit_gt import causal_check, repair_paths
from src.orchestration.fast_edit_gt import skill_instructions


def test_selected_skill_includes_complete_document_and_content_hash():
    import hashlib
    from src.orchestration.edit_skills import SKILLS_ROOT
    context=skill_instructions('Shopping Cart')
    source=(SKILLS_ROOT/context['name']/'SKILL.md').read_text()
    assert context['instructions']==source
    assert context['sha256']==hashlib.sha256(source.encode()).hexdigest()
    assert skill_instructions('not-a-known-type') is None


def test_repair_scope_retains_host_and_changed_components():
    before = [{'path':'src/App.tsx','code':'host'},{'path':'src/core.js','code':'stable'},
              {'path':'src/feature.jsx','code':'old'}]
    after = [before[0],before[1],{'path':'src/feature.jsx','code':'new'}]
    assert repair_paths(before,after) == ['src/App.tsx','src/feature.jsx']


def test_revised_flow_requires_interaction_followed_by_outcome():
    assert not causal_check({'actions':[{'action':'assert_visible','selector':'body'}]})
    assert not causal_check({'actions':[{'action':'assert_visible'},{'action':'click'}]})
    assert causal_check({'actions':[{'action':'click'},{'action':'assert_text'}]})
    assert causal_check({'actions':[{'action':'click'},{'action':'wait_for','selector':'.detail','state':'visible'}]})
    assert not causal_check({'actions':[{'action':'wait_for','selector':'body','state':'visible'}]})
    assert not causal_check(None)


def test_unattended_rejects_manual_resume_before_api(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from src.orchestration.fast_edit_gt import execute
    with pytest.raises(ValueError,match='fresh output'):
        asyncio.run(execute(SimpleNamespace(output=tmp_path,unattended=True,resume=True,
            browser_check=None,browser_url='http://127.0.0.1:1')))


def test_copy_and_local_patch_replay_preserves_all_source_files():
    source = [{'path':'index.html','code':'<main>old</main>\n'},
              {'path':'README.md','code':'original notes'}]
    refs = {'components/core.js':'export const enabled = true;\n'}
    target, patches = apply_candidate(source, {'copies':['components/core.js'],
        'patches':[{'path':'index.html','search':'old','replace':'new'}]}, refs, 'Edit')
    assert apply_patches(source, patches) == target
    assert {x['path']:x['code'] for x in target}['README.md'] == 'original notes'
    assert {p['task_type'] for p in patches} == {'Edit'}


def test_lenient_check_handles_lists_without_mutating_original():
    original = {'id':'flow','actions':[{'action':'wait_for','selector':'.item','state':'visible'},
        {'action':'scroll','y':800},{'action':'assert_count','selector':'.item','count':12}]}
    result = lenient_check(original)
    assert result['actions'][0]['selector'] == '.item:visible >> nth=0'
    assert result['actions'][1] == {'action':'key_press','selector':'body','key':'End'}
    assert result['actions'][2] == {'action':'assert_visible','selector':'.item:visible >> nth=0'}
    assert original['actions'][2]['action']=='assert_count'
    assert original['actions'][0]['selector'] == '.item'


def test_lenient_native_text_assertion_and_visible_actions():
    check = {'id':'login','actions':[
        {'action':'click','selector':'button[type=submit]'},
        {'action':'assert_text','selector':'.user-name:visible','value':'mentor','match':'contains'}]}
    normalized = lenient_check(check)
    assert normalized['actions'][0]['selector'] == 'button[type=submit]:visible'
    assert normalized['actions'][1]['selector'] == '.user-name'
    assert normalized['actions'][1]['match'] == 'nonempty'
    assert lenient_check(normalized) == normalized


def test_scoped_repair_preserves_unrelated_files_and_records_rejection(tmp_path):
    raw = {'copies':[], 'edits':[{'path':'src/main.jsx','start':1,'end':1,'text':'fixed'}],
           'new_files':[{'path':'src/library.mjs','code':'unrequested rewrite'}]}
    result = scoped_payload(raw, ['src/main.jsx'], tmp_path)
    assert result['edits'] == raw['edits']
    assert result['new_files'] == []
    assert raw['new_files']
    assert (tmp_path/'out_of_scope_edits.json').is_file()


@pytest.mark.parametrize('path',['../escape.js','/escape.js','.env','package.json'])
def test_candidate_rejects_out_of_scope_paths(path):
    with pytest.raises(ValueError):
        apply_candidate([{'path':'index.html','code':'<p>original</p>'}],
                        {'new_files':[{'path':path,'code':'x'}]}, {}, 'Edit')


def test_rejected_candidate_does_not_mutate_source():
    source = [{'path':'index.html','code':'<p>original</p>'}]
    with pytest.raises(ValueError):
        apply_candidate(source, {'patches':[
            {'path':'index.html','search':'original','replace':'changed'},
            {'path':'index.html','search':'absent','replace':'bad'}]}, {}, 'Edit')
    assert source[0]['code'] == '<p>original</p>'


def test_mistyped_directory_resolves_only_unique_same_filename_and_exact_snippet():
    source=[{'path':'src/App.jsx','code':'export default function App() { return <main>old</main>; }'}]
    payload={'patches':[{'path':'src/components/App.jsx','search':'<main>old</main>','replace':'<main>new</main>'}]}
    target,patches=apply_candidate(source,payload,{},'Edit')
    assert 'new' in target[0]['code']
    assert patches[0]['path']=='src/App.jsx'
    with pytest.raises(ValueError):
        apply_candidate(source+[{'path':'other/App.jsx','code':source[0]['code']}],payload,{},'Edit')


def test_repair_can_replace_its_new_adapter_but_not_original_source():
    code=[{'path':'Adapter.jsx','code':'bad adapter'},{'path':'App.jsx','code':'original app'}]
    payload={'new_files':[{'path':'Adapter.jsx','code':'fixed adapter'}]}
    target,patches=apply_candidate(code,payload,{},'Edit',replaceable_paths=['Adapter.jsx'])
    assert apply_patches(code,patches)==target
    with pytest.raises(ValueError):
        apply_candidate(code,{'new_files':[{'path':'App.jsx','code':'rewrite'}]}, {},'Edit',replaceable_paths=['Adapter.jsx'])


def test_references_follow_source_framework():
    code = [{'path':'package.json','code':'{"dependencies":{"react":"18"}}'},
            {'path':'src/App.jsx','code':'export default function App() {}'}]
    refs = references(code, 'Infinite Scroll')
    assert any(p.endswith('Component.jsx') for p in refs)
    assert any(p.endswith('infinite-scroll.mjs') for p in refs)
    assert not any(p.endswith('.vue') or p.endswith('.js') for p in refs)


def test_line_edits_use_original_coordinates_and_export_exact_patches():
    source = [{'path':'app.js','code':'const a = 1;\nconst b = 2;\nconst c = 3;\n'}]
    target, patches = apply_candidate(source, {'edits':[
        {'path':'app.js','start':1,'end':1,'text':'const a = 4;\n// same coordinate space\n'},
        {'path':'app.js','start':3,'end':3,'text':'const c = 5;\n'}]}, {}, 'Edit')
    assert target[0]['code'] == 'const a = 4;\n// same coordinate space\nconst b = 2;\nconst c = 5;\n'
    assert apply_patches(source, patches) == target


def test_small_host_replacement_exports_replayable_patch():
    source = [{'path':'app.jsx','code':'function App() {\n  return <main />;\n}\n'}]
    target, patches = apply_candidate(source, {'edits':[
        {'path':'app.jsx','start':1,'end':3,'text':'function App() {\n  return <main><Feed /></main>;\n}\n'}]}, {}, 'Edit')
    assert apply_patches(source, patches) == target
    large = [{'path':'app.jsx','code':''.join(f'// line {i}\n' for i in range(121))}]
    replacement = large[0]['code'].replace('// line 60\n','// changed line 60\n')
    target, patches = apply_candidate(large, {'edits':[{'path':'app.jsx','start':1,'end':121,'text':replacement}]}, {}, 'Edit')
    assert apply_patches(large, patches) == target
