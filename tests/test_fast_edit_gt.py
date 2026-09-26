import pytest
from src.orchestration.fast_edit_gt import apply_candidate, references, lenient_check, scoped_payload
from scripts.export_trajectory_dataset import apply_patches
from src.orchestration.fast_edit_gt import causal_check, repair_paths, review_source
from src.orchestration.fast_edit_gt import skill_instructions


def test_implementation_response_has_no_check_edit_authority():
    from src.orchestration.fast_edit_gt import response_format
    schema = response_format({}, implementation_only=True)['json_schema']['schema']
    assert set(schema['properties']) == {'copies','patches','new_files'}
    assert schema['additionalProperties'] is False


def test_duplicate_and_noop_patches_preserve_exact_replay():
    source = [{'path':'index.html','code':'<main>old</main>'}]
    patch = {'path':'index.html','search':'old','replace':'new'}
    target, edits = apply_candidate(source, {'copies':[], 'patches':[
        patch, dict(patch), {'path':'index.html','search':'new','replace':'new'}]}, {}, 'Edit')
    assert apply_patches(source, edits) == target
    assert target[0]['code'] == '<main>new</main>'


def test_whole_file_anchor_still_exports_local_diff():
    source = [{'path':'index.html','code':'<main>old</main>\n<footer>unchanged</footer>'}]
    target, edits = apply_candidate(source, {'copies':[], 'patches':[{
        'path':'index.html','search':source[0]['code'],
        'replace':source[0]['code'].replace('old','new')}]}, {}, 'Edit')
    assert apply_patches(source, edits) == target
    assert 'unchanged' in target[0]['code']


def test_react_source_without_package_uses_react_wrapper():
    refs = references([{'path':'src/App.tsx','code':'export default () => <main/>'}], 'Shopping Cart')
    assert any(p.endswith('/Component.jsx') for p in refs)


def test_copied_core_requires_host_dependency_path():
    code=[{'path':'index.html','code':'<main id="tree"></main>'}]
    refs={'components/tree.mjs':'export function mount() {}'}
    with pytest.raises(ValueError,match='not connected'):
        apply_candidate(code,{'copies':list(refs)},refs,'Tree View')
    payload={'copies':list(refs),'patches':[{'path':'index.html','search':'</main>',
        'replace':'</main><script type="module" src="adapter.mjs"></script>'}],
        'new_files':[{'path':'adapter.mjs','code':'import {mount} from "./components/tree.mjs"; mount();'}]}
    target, patches=apply_candidate(code,payload,refs,'Tree View')
    assert apply_patches(code,patches)==target


def test_classic_import_fails_before_browser_and_module_adapter_passes():
    from src.orchestration.fast_edit_gt import validate_classic_scripts
    original={'index.html':'<script src="app.js"></script>','app.js':'console.log("ready")'}
    invalid={**original,'app.js':'import {mount} from "./core.mjs"; mount();'}
    with pytest.raises(ValueError,match='classic script does not compile'):
        validate_classic_scripts(original,invalid)
    validate_classic_scripts(original,{**invalid,'index.html':'<script type="module" src="app.js"></script>'})


def test_product_repair_requires_exact_current_source_evidence():
    from src.orchestration.fast_edit_gt import has_defect_evidence
    code=[{'path':'app.js','code':'broken.mount(null);'}]
    assert not has_defect_evidence({'classification':'product_defect','reason':'review said no reuse'},code)
    assert not has_defect_evidence({'classification':'product_defect','defect_path':'app.js','defect_snippet':'made up'},code)
    assert has_defect_evidence({'classification':'product_defect','defect_path':'app.js','defect_snippet':'broken.mount(null)'},code)


def test_new_adapter_missing_import_resolves_only_unique_supplied_reference():
    code=[{'path':'index.html','code':'<main></main>'}]
    refs={'components/edit-skills/tree/core.mjs':'export function mount() {}'}
    payload={'copies':list(refs),'patches':[{'path':'index.html','search':'</main>',
        'replace':'</main><script type="module" src="components/edit-skills/adapter.mjs"></script>'}],
        'new_files':[{'path':'components/edit-skills/adapter.mjs',
            'code':'import {mount} from "./edit-skills/tree/core.mjs"; mount();'}]}
    target, patches=apply_candidate(code,payload,refs,'Tree View')
    assert 'from "./tree/core.mjs"' in next(x['code'] for x in target if x['path'].endswith('adapter.mjs'))
    assert apply_patches(code,patches)==target


def test_html_quote_repair_is_tag_local_and_preserves_script_strings(tmp_path):
    from src.orchestration.fast_edit_gt import normalize_html_quotes
    source='<button class="ab-btn"" data-panel="ai">AI</button><script>const s=\'<b class="x"">\';</script>'
    target=normalize_html_quotes([{'path':'index.html','code':source}],tmp_path)
    assert target[0]['code']=='<button class="ab-btn" data-panel="ai">AI</button><script>const s=\'<b class="x"">\';</script>'


def test_staged_candidate_keeps_valid_files_for_wiring_only_recovery():
    from src.orchestration.fast_edit_gt import UnmountedCandidate, require_core_connection
    code=[{'path':'index.html','code':'<main></main>'}]
    refs={'components/core.mjs':'export function mount() {}'}
    with pytest.raises(UnmountedCandidate) as error:
        apply_candidate(code,{'copies':list(refs),'new_files':[{
            'path':'adapter.mjs','code':'import {mount} from "./components/core.mjs"; mount();'}]},refs,'Edit')
    staged=error.value.target
    target,_=apply_candidate(staged,{'copies':[],'patches':[{'path':'index.html','search':'</main>',
        'replace':'</main><script type="module" src="adapter.mjs"></script>'}]},{},'Edit')
    require_core_connection(target,refs)
    assert next(x['code'] for x in target if x['path']=='adapter.mjs')==next(x['code'] for x in staged if x['path']=='adapter.mjs')


def test_selected_skill_includes_complete_document_and_content_hash():
    import hashlib
    from src.orchestration.edit_skills import SKILLS_ROOT, shared_edit_skill_contract
    context=skill_instructions('Shopping Cart')
    source=(SKILLS_ROOT/context['name']/'SKILL.md').read_text()
    effective=shared_edit_skill_contract()+'\n\n'+source
    assert context['instructions']==effective
    assert context['sha256']==hashlib.sha256(effective.encode()).hexdigest()
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


def test_vanilla_references_have_real_module_exports():
    refs=references([{'path':'index.html','code':'<main></main>'}],'Tree View')
    module=next(code for path,code in refs.items() if path.endswith('tree.mjs'))
    assert 'export { mountTreeView }' in module
    assert not any(path.endswith(('.jsx','.vue','.js')) for path in refs)


def test_reviewer_sees_unchanged_entry_dependencies():
    before=[{'path':'index.html','code':'<script type="module" src="js/app.js"></script>'},
        {'path':'js/app.js','code':"import {open} from './views/panel'; open();"},
        {'path':'js/views/panel.js','code':'export function open() {}'},
        {'path':'styles.css','code':'body {}'}]
    after=before+[{'path':'adapter.mjs','code':'export const adapter = 1'}]
    assert {x['path'] for x in review_source(before,after)}=={
        'index.html','js/app.js','js/views/panel.js','adapter.mjs'}


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
