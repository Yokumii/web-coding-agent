"""Preloaded, single-call Edit implementation using the native Harness runtime."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path

from src.agents.openai_runner import OpenAIHTTPClient
from src.orchestration.edit_skills import EDIT_SKILLS, SKILLS_ROOT
from src.orchestration.skill_rsi import enqueue as enqueue_skill_learning, helper_context
from scripts.export_trajectory_dataset import apply_patches, make_patches


SYSTEM = '''Implement this ONE supplied Edit instruction completely in the existing project.
Preserve unrelated behavior, layout, routes, framework and existing data identities. No dependencies.
Keep every existing mounted feature and its entry controls. Mount new features in the actual
rendered host; creating an unreferenced file does not implement an instruction.
Keep changes small: prefer a separate adapter component and minimal host wiring.
Reference components own mount, cleanup and reconfiguration. Never destroy their API in a parent
effect body: effects run on initial mount too. Keep configure callbacks stable and avoid feedback
loops where an onChange update changes configure identity. Cleanup belongs in effect return functions.
The source and matching reusable component are preloaded. Do not plan tool calls or explain steps.
Reuse ALL supplied reference files via copies and import/mount their public API in the host.
Do not independently rewrite pagination, concurrency, retries or lifecycle already handled by the core.
Implement all additional explicit requirements locally.
Do not reduce instruction coverage to fit a line budget. Use compact, readable host integration.
Mock records/services are allowed only when requested; derive identities from the supplied data.
Return JSON: {"copies":["destination path"], "patches":[{"path":"existing path",
"search":"exact unique original source substring", "replace":"replacement source"}],
"new_files":[{"path":"new local component path", "code":"complete new source"}],
"browser_check":{"id":"current-edit", "route":"/", "actions":[...]}}.
Copies happen before patches. Each search must match exactly once, preserving whitespace.
Use small nonoverlapping complete logical blocks; never approximate line numbers or snippets.
Paths are project-relative. Preserve ALL unrelated source content.
Do not emit copied reference contents. A copied file may then be locally patched.
Reuse actual existing datasets rather than hardcoded copies of their names. Keep generated code concise.
Before answering, ensure every explicit requirement is covered. A reference's default labels,
loading UI, initial-load trigger and history behavior may need small local adaptations. In particular:
loading TEXT is not a skeleton; use a skeleton during the real async load; required labels must match;
load the first page when mounted, bind the observer root to the actual scroll container, retain loaded
items and scroll position across any requested detail/back interaction. Clean up async work on unmount.
Do not invent browser selectors. Use ONLY selectors present in the source/copies/edits you return.
For asynchronous counts, wait_for the last expected item before asserting the count.
Use ONE concise continuous browser flow to exercise the feature, including requested failure/retry.
Every action is an object with an action field, e.g. {"action":"click","selector":"#toggle"}.
Actions: click(selector), fill(selector,value), select_option(selector,value),
scroll(y) scrolls the WINDOW to an absolute position; key_press(selector,key) scrolls a focusable container.
wait_for(selector,state=visible), assert_count(selector,count),
assert_text(selector,value,match=contains|exact), assert_visible(selector),
assert_value(selector,value), assert_no_console_errors. Wait for asynchronous content with wait_for.
Use stable selectors, real input actions and state-changing assertions, not mere element presence.
Keep browser flow under 20 seconds. No sleeps, scripted state changes or test-only behavior.
Return only the complete JSON object; no markdown or commentary.'''


def response_format(refs, frozen_check=None, lenient=False, source_paths=None):
    def obj(fields):
        return {'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}
    string = {'type':'string'}
    integer = {'type':'integer'}
    def action(name, fields):
        return obj({'action':{'type':'string','enum':[name]}, **fields})
    actions = [action('click', {'selector':string}), action('fill', {'selector':string,'value':string}),
        action('select_option', {'selector':string,'value':string}), action('scroll', {'y':integer}),
        action('key_press', {'selector':string,'key':string}), action('wait_for', {'selector':string,'state':string}),
        action('assert_count', {'selector':string,'count':integer}),
        action('assert_text', {'selector':string,'value':string,'match':string}),
        action('assert_value', {'selector':string,'value':string}), action('assert_visible', {'selector':string}),
        action('assert_no_console_errors', {})]
    schema = obj({'copies':{'type':'array','items':{'type':'string', **({'enum':list(refs)} if refs else {})},
                            'minItems':len(refs),'maxItems':len(refs)},
        'patches':{'type':'array','items':obj({'path':{'type':'string',**({'enum':sorted(set(source_paths)|set(refs))} if source_paths else {})},'search':string,'replace':string})},
        'new_files':{'type':'array','items':obj({'path':string,'code':string})},
        'browser_check':obj({'id':string,'route':string,'actions':{'type':'array','items':{'anyOf':actions},'minItems':3,'maxItems':5 if lenient else 12}})})
    if frozen_check:
        schema['properties'].pop('browser_check')
        schema['required'].remove('browser_check')
    return {'type':'json_schema','json_schema':{'name':'fast_edit_gt','strict':True,'schema':schema}}


def digest(code):
    return hashlib.sha256(json.dumps(sorted(code, key=lambda x: x['path']),
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def lenient_check(check):
    check = json.loads(json.dumps(check))
    for action in check['actions']:
        if action['action']=='assert_count' and action.get('count',0)>0:
            selector=action['selector']
            action.clear()
            action.update(action='assert_visible',selector=f'{selector} >> nth=0')
        selector = action.get('selector')
        if selector and action['action']=='assert_text':
            action['selector'] = selector.replace(':visible','')
            action.update(match='nonempty', value='')
        visible_action = action['action'] in {'click','fill','select_option','key_press','assert_visible'} or (
            action['action']=='wait_for' and action.get('state','visible')=='visible')
        if selector and visible_action and not selector.startswith(('text=','xpath=','//','role=')):
            pieces = selector.split(' >> ')
            if not pieces[0].endswith(':visible'):
                pieces[0] += ':visible'
            action['selector'] = ' >> '.join(pieces)
        if action['action'] == 'wait_for' and action.get('state','visible') in {'visible','attached'}:
            if not action['selector'].endswith(' >> nth=0'):
                action['selector'] += ' >> nth=0'
        if action['action'] == 'scroll' and action.get('y',0) > 0:
            action.clear()
            action.update(action='key_press',selector='body',key='End')
    return check


def normalize_css(bundle, frontend, output):
    """Remove only compiler-proven unmatched CSS closing braces; keep all other errors."""
    script = r'''
const fs=require('fs'),postcss=require('postcss');
const bundle=JSON.parse(fs.readFileSync(0,'utf8')), fixes=[];
for (const file of bundle) {
  if (!file.path.endsWith('.css')) continue;
  let candidate=file.code, removed=[];
  for(let i=0;i<4;i++) {
    try { postcss.parse(candidate,{from:file.path}); file.code=candidate; fixes.push(...removed); break; }
    catch(e) {
      if(e.reason!=='Unexpected }' || i===3) break;
      const lines=candidate.split('\n');
      const offset=lines.slice(0,e.line-1).reduce((n,s)=>n+s.length+1,0)+e.column-1;
      if(candidate[offset]!=='}') break;
      removed.push({path:file.path,line:e.line,reason:e.reason});
      candidate=candidate.slice(0,offset)+candidate.slice(offset+1);
    }
  }
}
process.stdout.write(JSON.stringify({bundle,fixes}));
'''
    process = subprocess.run(['node','-e',script], input=json.dumps(bundle), text=True,
        capture_output=True, cwd=frontend, timeout=5)
    if process.returncode:
        return bundle
    result = json.loads(process.stdout)
    if result['fixes']:
        save(output/'format_repairs.json', result['fixes'])
    return result['bundle']


def scoped_payload(payload, focus_paths, output):
    if not focus_paths:
        return payload
    payload = json.loads(json.dumps(payload))
    ignored = []
    for kind in ('edits','new_files','patches'):
        ignored.extend({'kind':kind,'path':x['path']} for x in payload.get(kind,[]) if x['path'] not in focus_paths)
        if kind in payload:
            payload[kind] = [x for x in payload[kind] if x['path'] in focus_paths]
    if ignored:
        save(output/'out_of_scope_edits.json', ignored)
    return payload


def repair_paths(before, after):
    original = {x['path']: x['code'] for x in before}
    return [x['path'] for x in after if x['code'] != original.get(x['path'])
            or Path(x['path']).name in {'App.jsx','App.tsx','App.vue','main.jsx','main.tsx','index.html'}]


def causal_check(check):
    actions = check.get('actions', []) if isinstance(check, dict) else []
    interaction = False
    for action in actions:
        if action.get('action') in {'click','fill','select_option','key_press','scroll'}:
            interaction = True
        elif interaction and (action.get('action') in {'assert_text','assert_visible','assert_value','assert_count'}
                or action.get('action')=='wait_for' and action.get('state','visible') in {'visible','hidden','detached'}):
            return True
    return False




def skill_instructions(task_type):
    name = EDIT_SKILLS.get(task_type)
    if not name:
        return None
    path = SKILLS_ROOT / name / 'SKILL.md'
    text = path.read_text(encoding='utf-8')
    extra,_ = helper_context(path.parent)
    text += extra
    return {'name':name,'instructions':text,
            'sha256':hashlib.sha256(text.encode()).hexdigest()}


async def review_skill_reuse(*, before, after, item, config, output):
    """Semantic source review of actual core usage, not cosmetic acceptance."""
    skill = skill_instructions(item['task_type'])
    if not skill:
        return {'reused':True,'reason':'no selected reference skill','focus_paths':[]}
    output.mkdir(exist_ok=True)
    paths = set(repair_paths(before,after))
    source = [x for x in after if x['path'] in paths or f"/{skill['name']}/" in x['path']]
    schema = {'type':'object','properties':{'reused':{'type':'boolean'},'reason':{'type':'string'},
        'focus_paths':{'type':'array','items':{'type':'string'}}},
        'required':['reused','reason','focus_paths'],'additionalProperties':False}
    system = '''Check actual reuse of the supplied Skill core in this generated Edit. Trace the visible
user action through handlers and state to the supplied reference API. Pass normal data/service/style
adapters and necessary small core extensions. This is NOT a cosmetic or exhaustive feature review.
If user actions really call core.go/update/add/authenticate and that core drives the feature, PASS
reuse even if an additional animation style or other secondary requirement is incomplete. Do not
mislabel incomplete extensions as core bypass. Only reject missing genuine core usage or a duplicate
engine replacing it. The browser handles functional checks separately.
Fail concrete bypasses: import never called; dummy hidden reference while another engine owns the
feature; authenticate callback returning null while a separate submit handler sets the session;
copying the core algorithm into an adapter instead of calling it. Hidden closed popovers are normal
when their real controls use the core. A headless createCart used for real validation/calculation is
valid; React may own serialized state and business capacity constraints. Cite exact source statements
and minimal repair paths. A model comment claiming reuse is not evidence. Do not require use of all
copied wrapper variants; directly invoking the exported core is sufficient. Return JSON only.'''
    request={'instruction':item,'skill':skill,'source':source}
    save(output/'request.json',request)
    metric={'model':config.generator_model,'status':'started','usage':None,'purpose':'skill_reuse_review'}
    started=time.monotonic()
    try:
        response=await OpenAIHTTPClient(config,30).complete(model=config.generator_model,
            messages=[{'role':'system','content':system},{'role':'user','content':json.dumps(request,ensure_ascii=False)}],
            max_tokens=1200,**({'_stream':True,'enable_thinking':False} if config.generator_model.startswith('qwen') else {'reasoning_effort':'low'}),
            response_format={'type':'json_schema','json_schema':{'name':'skill_reuse_review','strict':True,'schema':schema}})
        metric.update(status='completed',usage=response.get('usage'))
        result=json.loads(response['choices'][0]['message']['content'])
        save(output/'review.json',result)
        return result
    except BaseException as exc:
        metric.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        metric['elapsed_seconds']=round(time.monotonic()-started,3)
        save(output/'call_1.json',metric)


def references(code, task_type):
    name = EDIT_SKILLS.get(task_type)
    if not name:
        return {}
    mapping = {x['path']: x['code'] for x in code}
    package = json.loads(mapping.get('package.json', '{}'))
    deps = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    stack = 'react' if 'react' in deps else 'vue' if 'vue' in deps else 'vanilla'
    base = 'src/components' if any(p.startswith('src/') for p in mapping) else 'components'
    result = {}
    for p in sorted((SKILLS_ROOT / name / 'references').iterdir()):
        if (p.suffix == '.css' or (stack == 'vanilla' and p.suffix == '.js') or
            (stack != 'vanilla' and p.suffix == '.mjs') or
            p.name == {'react': 'Component.jsx', 'vue': 'Component.vue'}.get(stack)):
            destination = f'{base}/edit-skills/{name}/{p.name}'
            if destination not in mapping:
                result[destination] = p.read_text()
    _,helpers=helper_context(SKILLS_ROOT/name)
    filename='learned.js' if stack=='vanilla' else 'learned.mjs'
    destination=f'{base}/edit-skills/{name}/{filename}'
    if filename in helpers and destination not in mapping:
        result[destination]=helpers[filename]
    return result


def apply_candidate(code, payload, refs, task_type, replaceable_paths=None):
    original = {x['path']: x['code'] for x in code}
    current = dict(original)
    if set(payload.get('copies', [])) != set(refs):
        raise ValueError('reuse the selected reference copies instead of rewriting their implementation')
    def safe(path):
        if not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('unsafe patch path')
        if any(p.startswith('.') or p == 'node_modules' for p in Path(path).parts):
            raise ValueError('hidden/runtime paths cannot be edited')
        if Path(path).name in {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'}:
            raise ValueError('dependency changes are not allowed')
        return path
    for path in payload.get('copies', []):
        safe(path)
        if path not in refs or path in current:
            raise ValueError(f'invalid reference copy: {path}')
        current[path] = refs[path]
    for item in payload.get('new_files', []):
        path = safe(item['path'])
        if (path in current and path not in (replaceable_paths or [])) or not isinstance(item.get('code'), str) or not item['code'].strip():
            raise ValueError(f'invalid new file: {path}')
        current[path] = item['code']
    for item in payload.get('patches', []):
        path = safe(item['path'])
        before = current.get(path)
        search, replace = item['search'], item['replace']
        if before is None and search:
            matches = [p for p,s in current.items() if Path(p).name==Path(path).name and s.count(search)==1]
            if len(matches)==1:
                path = safe(matches[0])
                before = current[path]
        if before is None or not search or before.count(search) != 1 or search == before:
            raise ValueError(f'search must be a unique local snippet in {path}: {str(search)[:100]}')
        current[path] = before.replace(search, replace, 1)
    by_path = {}
    for edit in payload.get('edits', []):
        by_path.setdefault(safe(edit['path']), []).append(edit)
    for path, edits in by_path.items():
        if path not in current:
            raise ValueError(f'line edits target a missing file: {path}')
        lines = current[path].splitlines(keepends=True)
        boundary = len(lines) + 1
        for edit in sorted(edits, key=lambda x:x['start'], reverse=True):
            start, end = edit['start'], edit['end']
            if not 1 <= start <= end+1 <= boundary or end > len(lines):
                raise ValueError(f'invalid/overlapping line range: {path}:{start}-{end}')
            lines[start-1:end] = [edit['text']]
            boundary = start
        current[path] = ''.join(lines)
    target = [{'path': p, 'code': current[p]} for p in sorted(current)]
    patches = make_patches(code, target, task_type)
    if not patches or apply_patches(code, patches) != target:
        raise ValueError('empty or non-replayable Edit')
    return target, patches


async def generate_step(*, code, item, config, output, timeout=180, feedback=None, max_attempts=3, frozen_check=None, lenient=False, prior_checks=None):
    started = time.monotonic()
    refs = references(code, item['task_type'])
    selected_skills = [skill_instructions(item['task_type'])]
    failed_type = (feedback or {}).get('failed_instruction',{}).get('task_type')
    if failed_type and failed_type != item['task_type']:
        selected_skills.append(skill_instructions(failed_type))
    selected_skills = [s for s in selected_skills if s]
    save(output/'skill_context.json',selected_skills)
    # No metadata/readme/lockfiles in model context. Preserve ALL original files in GT.
    visible = [x for x in code if not any(p.startswith('.') for p in Path(x['path']).parts)
               and Path(x['path']).suffix.lower() not in {'.md', '.lock'}]
    if feedback and feedback.get('focus_paths'):
        visible = [x for x in visible if x['path'] in feedback['focus_paths'] or x['path']=='package.json']
    user = json.dumps({'instruction': item,
        'selected_skill_documents':selected_skills,
        'current_source':visible,
        'available_reference_copies': refs,
        'failure_feedback':feedback, 'frozen_browser_check':frozen_check,
        'preserve_existing_browser_flows':prior_checks or []}, ensure_ascii=False)
    save(output / 'request.json', {'model': config.generator_model,
        'enable_thinking': True if config.generator_model.startswith('qwen') else None,
        'thinking_budget': 1024 if config.generator_model.startswith('qwen') else None,
        'reasoning_effort': None if config.generator_model.startswith('qwen') else 'low',
        'stream': True, 'store': False, 'instruction': item, 'source_sha256': digest(code),
        'reference_sha256': {p: hashlib.sha256(s.encode()).hexdigest() for p,s in refs.items()},
        'skill_document_sha256':{s['name']:s['sha256'] for s in selected_skills},
        'timeout_seconds': timeout, 'max_attempts': max_attempts})
    system = SYSTEM
    system += '''\nThe selected SKILL.md documents are part of your implementation instructions, not optional
background. Use their public API and host integration guidance. This runner's JSON schema and lenient
browser policy override legacy copy_from/line_edits response examples and stricter check suggestions.
Real user actions MUST go through the supplied core implementation. Copying/importing a reference or
mounting it hidden while another implementation owns the business state is NOT reuse. Do not create
a duplicate cart/auth engine in an adapter. For custom cart UI, use exported createCart as the state
and arithmetic owner; add missing generic operations locally if required. For authentication, wire
the real form through mountAuthentication and host authenticate/restoreSession/onSession callbacks;
do not mount a dummy authenticate callback and implement a separate submit/session flow.
Adapters map existing data, refs, styles and service callbacks. Extend missing core capabilities
minimally rather than bypassing the core. Ordinary closed popovers may be hidden; dummy reference
mounts that never drive user-facing behavior are forbidden. Preserve other completed features.'''
    if feedback:
        system += '''\nDiagnose and repair in this same response. Trace the actual mount/effect/cleanup/data
execution path in the provided source. Earlier diagnoses are hypotheses, not facts. Do not repeat a
failed patch. Fix the root cause with a minimal exact source replacement, preserving other features.
The supplied failed_browser_check should remain unchanged UNLESS concrete source/DOM evidence shows
a wrong selector, premature assertion, or secondary requirement beyond lenient acceptance. In that
case correct the check while retaining its main user interaction and observable outcome. Do not make
product code imitate tests. If only the check is wrong return empty patches/new_files and the corrected
browser_check. Return the check for the FAILED subtask, which may be an earlier regression flow.'''
        system += '''\nA feature behind a valid tab is not missing: navigate there before testing it.
Do not alter the default product view to satisfy a stale check. Extra loaded records and incidental
wording are not defects. Retain the real main outcome; never claim success without browser replay.'''
    if frozen_check:
        system = SYSTEM.split('Use ONE concise continuous browser flow')[0] + '''
The browser flow is supplied and frozen. Do NOT generate tests or a browser_check field.
Use its selectors as the host integration contract. Implement ALL instruction requirements,
including requirements not checked by this smoke flow. You MUST import and mount the new feature
in the EXISTING rendered page, not merely create files. Copy the references unchanged;
use their optional parameters for initial loading, end text and skeletons.
The instruction defines product behavior; never invent a test-specific fixture or fake data
solely to satisfy assertions. Use the actual scroll container as the observer root.
Do not invent reference API methods. Reset by reconfiguring/remounting on filter change.
Support record detail via a history entry and popstate close without unmounting the feed.
No TODOs, placeholders, fake handlers or comments claiming a feature instead of implementing it.
For keyboard scrolling, make the actual scroll container focusable.
Spend output on concise integration, not explanatory comments or large CSS blocks.
Return only JSON conforming to the supplied schema.'''
    if lenient:
        system += '''\nAcceptance is intentionally LENIENT: implement the instruction, but test only page
rendering and ONE main user interaction with a visible result. Use 3-5 actions total.
Accept equivalent UI behavior. Do not test exact numbers, cosmetic details, exact wording,
timing, failure simulations, or secondary features. Prefer assert_visible on the result
of an actual click, or assert_text to check nonempty result content. Use unique selectors
(append >> nth=0 when selecting one of many). Minor shortcomings are notes, not blockers.
Do not add test-specific behavior to the product. A blank page, missing main feature,
or broken main interaction still fails. Keep the original instruction's requested functionality.'''
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    attempts = []
    for attempt in range(1, max_attempts + 1):
        remaining = timeout - (time.monotonic() - started)
        if remaining < 5:
            raise TimeoutError('subtask deadline exhausted')
        options = ({'_stream': True, 'enable_thinking': True, 'thinking_budget': 1024}
                   if config.generator_model.startswith('qwen') else {'reasoning_effort': 'low'})
        call_started = time.monotonic()
        metric = {'model':config.generator_model, 'status':'started', 'usage':None}
        metric_path = output/f'call_{attempt}.json'
        save(metric_path, metric)
        try:
            response = await OpenAIHTTPClient(config, remaining).complete(
                model=config.generator_model, messages=messages, max_tokens=8000,
                **options, response_format=response_format(refs, frozen_check, lenient, [x['path'] for x in code]))
            metric.update(status='completed', usage=response.get('usage'),
                elapsed_seconds=round(time.monotonic()-call_started,3))
        except BaseException as exc:
            metric.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                elapsed_seconds=round(time.monotonic()-call_started,3))
            raise
        finally:
            save(metric_path, metric)
        raw = response['choices'][0]['message']['content']
        (output / f'response_{attempt}.json').write_text(raw, encoding='utf-8')
        attempts.append({'attempt': attempt, 'usage': response.get('usage', {}),
                         'elapsed_seconds': round(time.monotonic()-started, 3)})
        save(output / 'attempts.json', attempts)
        payload = {}
        try:
            payload = json.loads(raw)
            payload = scoped_payload(payload, (feedback or {}).get('focus_paths'), output)
            for action in payload.get('browser_check', {}).get('actions', []):
                for key in list(action):
                    if action[key] is None:
                        del action[key]
            if feedback and not refs and not any(payload.get(k) for k in ('copies','edits','patches','new_files')):
                target, patches = code, []
            else:
                target, patches = apply_candidate(code, payload, refs, item['task_type'],
                    replaceable_paths=(feedback or {}).get('replaceable_paths'))
            if not causal_check(frozen_check or payload.get('browser_check')):
                raise ValueError('browser flow must exercise a real interaction and assert its visible outcome')
            return {'target': target, 'patches': patches, 'browser_check': frozen_check or payload.get('browser_check'),
                    'attempts': attempts, 'generation_seconds': round(time.monotonic()-started, 3)}
        except (ValueError, KeyError, TypeError) as exc:
            attempts[-1]['error'] = str(exc)
            save(output / 'attempts.json', attempts)
            if attempt == max_attempts:
                raise
            paths = {p['path'] for p in payload.get('patches',[]) if p.get('search') and
                     (next((x['code'] for x in code if x['path']==p['path']),refs.get(p['path'],''))).count(p['search'])!=1}
            repair_source = {x['path']:x['code'] for x in code if x['path'] in paths}
            repair_source.update({p:s for p,s in refs.items() if p in paths})
            messages += [{'role': 'assistant', 'content': raw}, {'role': 'user', 'content':
                f'Candidate was NOT applied: {exc}. No edits from this response were committed. '
                'Return corrected complete JSON against SAME original source. Never target code from '
                'an earlier rejected answer. If a change is already included inside an earlier replacement '
                'in your response, remove the redundant second patch. Use short exact unique anchors. '
                'Here are the unmodified files implicated in search mismatches: '+json.dumps(repair_source,ensure_ascii=False)}]


async def execute(args):
    from scripts.run_g2_compound_edit import _read_case, normalize_input_case
    from src.config import HarnessConfig
    from src.orchestration.browser_evidence import collect_browser_evidence
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    unattended = getattr(args,'unattended',False)
    if unattended and (args.resume or args.browser_check or not args.browser_url
            or any(output.glob('step_*')) or (output/'input.json').exists()):
        raise ValueError('unattended validation requires fresh output, live browser, no resume or manual checks')
    run_started = time.monotonic()
    if (output / 'input.json').exists() and not args.resume:
        raise ValueError('use a fresh output directory; completed checkpoints are preserved')
    case = normalize_input_case(_read_case(args.case))
    if args.resume:
        if json.loads((output/'input.json').read_text()) != case:
            raise ValueError('resume input differs from original case')
        if (output/'result.json').exists():
            save(output/'result_before_resume.json', json.loads((output/'result.json').read_text()))
    save(output / 'input.json', case)
    code = case['source_code']
    # Preserve source paths including a shared code/ prefix; only strip it internally.
    prefix = 'code/' if all(x['path'].startswith('code/') for x in code) else ''
    original = sorted([{'path': x['path'][len(prefix):], 'code': x['code']} for x in code], key=lambda x:x['path'])
    current = original
    qwen = args.provider_profile == 'qwen'
    profile = ({'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1'} if qwen else
        json.loads((Path.home()/'.config/webcoding/experimental-luna.json').read_text()))
    credential = 'qwen-dashscope-openai.key' if qwen else 'njulink-degraded.key'
    key = (Path.home()/'.config/webcoding/credentials'/credential).read_text().strip()
    config = HarnessConfig(agent_runtime='openai', openai_api_key=key,
        openai_base_url=profile['base_url'].rstrip('/'), openai_wire_api='chat' if qwen else 'responses',
        openai_extra_headers=profile.get('http_headers', {}), openai_stream_read_retries=0,
        generator_model=args.model or ('qwen3.7-max' if qwen else 'gpt-5.6-luna'))
    frontend = output / 'frontend'
    frontend.mkdir(exist_ok=True)
    def materialize(bundle):
        for x in bundle:
            path = (frontend / x['path']).resolve()
            path.relative_to(frontend)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file() or path.read_text(encoding='utf-8') != x['code']:
                path.write_text(x['code'], encoding='utf-8')
    materialize(original)
    baseline_errors = []
    if args.browser_url:
        async with asyncio.timeout(20):
            baseline = await collect_browser_evidence(app_url=args.browser_url,
                checks=[{'id':'source-baseline','route':'/','actions':[
                    {'action':'assert_visible','selector':'body'}]}],
                output_path=output/'source_browser.json', headless=True)
        for checked in baseline['checks']:
            if checked.get('navigation_error'):
                raise ValueError('source preview is not running')
            baseline_errors.extend(checked.get('console_errors', []))
    tasks = case['descriptions']
    reuse_cache = {}
    async def verify_flow(check, directory, index):
        merged = {'checks':[]}
        key=(index,digest(generated['target']))
        if key not in reuse_cache:
            reuse_cache[key]=await review_skill_reuse(before=current,after=generated['target'],
                item=tasks[index-1],config=config,output=directory/'reuse_review')
        review=reuse_cache[key]
        merged['skill_reuse']=review
        if not review['reused']:
            merged['checks']=[{'status':'skill_reuse_failed','subtask_index':index,
                'steps':[],'reason':review['reason'],'focus_paths':review['focus_paths']}]
            save(directory/'browser_evidence.json',merged)
            return merged
        flows = [(index, check)]
        for prior in range(1,index):
            previous_check = json.loads((output/f'step_{prior:02d}'/'browser_check.json').read_text())
            flows.append((prior, lenient_check(previous_check) if args.acceptance=='lenient' else previous_check))
        for number, flow in flows:
            evidence = await collect_browser_evidence(app_url=args.browser_url, checks=[flow],
                output_path=directory/f'flow_{number:02d}.json',headless=True,fail_fast=True,
                action_timeout_ms=3000,baseline_console_errors=baseline_errors,
                lenient_console=args.acceptance=='lenient')
            merged['checks'].extend({**c,'subtask_index':number} for c in evidence['checks'])
            if not evidence['checks'] or any(c['status']!='ok' for c in evidence['checks']):
                break
        save(directory/'browser_evidence.json', merged)
        return merged
    selected = tasks[:args.max_steps] if args.max_steps else tasks
    results, patches = [], []
    for index, item in enumerate(selected, 1):
        step = output / f'step_{index:02d}'
        step.mkdir(exist_ok=args.resume)
        previous = json.loads((step/'result.json').read_text()) if args.resume and (step/'result.json').exists() else {}
        if previous.get('status') == 'ok':
            checkpoint = json.loads((step/'checkpoint.json').read_text())
            if checkpoint['source_sha256'] != digest(original) or apply_patches(original, checkpoint['patches']) != checkpoint['target']:
                raise ValueError('accepted checkpoint source or patch replay mismatch')
            if args.browser_url and not checkpoint['browser_verified']:
                raise ValueError('cannot resume unverified checkpoint as accepted')
            current, patches = checkpoint['target'], checkpoint['patches']
            results.append(previous)
            materialize(current)
            print(json.dumps({'index':index,'status':'reused_accepted_checkpoint'}, ensure_ascii=False), flush=True)
            continue
        if previous:
            save(step/'result_before_resume.json', previous)
        prior_elapsed = previous.get('elapsed_seconds', 0)
        started = time.monotonic() - prior_elapsed
        save(output/'worker_state.json', {'status':'running','subtask_index':index,
            'deadline':time.time()+max(0,args.subtask_timeout-prior_elapsed)})
        try:
            async with asyncio.timeout(max(0, args.subtask_timeout-prior_elapsed)):
                if previous:
                    folders = [step] + sorted((p for p in step.glob('repair_*') if p.is_dir()),
                        key=lambda p:int(p.name.split('_')[-1]))
                    bundles = []
                    for folder in folders:
                        if (folder/'generated.json').is_file():
                            bundles.append(json.loads((folder/'generated.json').read_text()))
                            continue
                        responses = sorted(folder.glob('response_*.json'))
                        if not responses:
                            continue
                        base = bundles[-1]['target'] if bundles else current
                        payload = json.loads(responses[-1].read_text())
                        if not any(payload.get(k) for k in ('copies','edits','new_files','patches')):
                            continue
                        if (step/'repair_context.json').exists() and folder != step:
                            payload = scoped_payload(payload, json.loads((step/'repair_context.json').read_text()).get('focus_paths'), folder)
                        target, recovered_patches = apply_candidate(base, payload, references(base,item['task_type']), item['task_type'])
                        attempts = json.loads((folder/'attempts.json').read_text())
                        recovered = {'target':target,'patches':recovered_patches,
                            'attempts':attempts,'generation_seconds':attempts[-1]['elapsed_seconds'],
                            'browser_check':bundles[0]['browser_check'] if bundles else payload.get('browser_check')}
                        save(folder/'generated.json', recovered)
                        bundles.append(recovered)
                    if not bundles:
                        raise ValueError('no complete generated candidate to resume')
                    generated = {**bundles[-1],
                        'patches':make_patches(current, bundles[-1]['target'], item['task_type']),
                        'attempts':[a for b in bundles for a in b['attempts']],
                        'generation_seconds':sum(b['generation_seconds'] for b in bundles)}
                    if args.browser_check:
                        generated['browser_check'] = json.loads(args.browser_check.read_text())
                else:
                    generated = await generate_step(code=current, item=item, config=config, output=step,
                        timeout=args.subtask_timeout - (20 if args.browser_url else 1),
                        lenient=args.acceptance == 'lenient',
                        prior_checks=[json.loads((output/f'step_{n:02d}'/'browser_check.json').read_text())
                                      for n in range(1,index)],
                        frozen_check=json.loads(args.browser_check.read_text()) if args.browser_check else None)
                    save(step/'generated.json', generated)
                generated['target'] = normalize_css(generated['target'], frontend, step)
                generated['patches'] = make_patches(current, generated['target'], item['task_type'])
                materialize(generated['target'])
                evidence = None
                if args.browser_url:
                    check = generated['browser_check']
                    if not unattended and (step/'check_reviewed.json').is_file():
                        check = json.loads((step/'check_reviewed.json').read_text())
                    if check and args.acceptance == 'lenient':
                        check = lenient_check(check)
                    if not check or not check.get('actions'):
                        raise ValueError('missing causal browser check')
                    save(step / 'browser_check.json', check)
                    evidence = await verify_flow(check, step, index)
                    repair_history = []
                    for repair_index in range(1, 5):
                        if evidence['checks'] and all(x['status']=='ok' for x in evidence['checks']):
                            break
                        failed = next(x for x in evidence['checks'] if x['status']!='ok')
                        failed_index = failed['subtask_index']
                        failed_check_path = output/f'step_{failed_index:02d}'/'browser_check.json'
                        failed_check = json.loads(failed_check_path.read_text())
                        if repair_index==4:
                            break
                        remaining = args.subtask_timeout - (time.monotonic()-started) - 10
                        if remaining < 10:
                            raise ValueError('browser check failed; no repair time remains')
                        next_index = 1 + max([int(p.name.split('_')[-1]) for p in step.glob('repair_*') if p.is_dir()] or [0])
                        repair_dir = step/f'repair_{next_index}'
                        repair_dir.mkdir()
                        repaired = await generate_step(code=generated['target'], item=item,
                            config=config, output=repair_dir, timeout=remaining, max_attempts=2,
                            lenient=args.acceptance == 'lenient',
                            feedback={'browser_failure':evidence,'failed_browser_check':failed_check,
                                'failed_instruction':tasks[failed_index-1],
                                'previous_repairs':repair_history,
                                'replaceable_paths':[x['path'] for x in generated['target']
                                    if x['path'] not in {p['path'] for p in current} and '/edit-skills/' not in x['path']],
                                'prior_contracts':[{'instruction':tasks[n-1],
                                    'check':json.loads((output/f'step_{n:02d}'/'browser_check.json').read_text())}
                                    for n in range(1,index)],
                                'focus_paths':repair_paths(current,generated['target']),
                                **(json.loads((step/'repair_context.json').read_text()) if not unattended and (step/'repair_context.json').exists() else {}),
                                'instruction':'Repair the observed root cause locally; retain a real main interaction and outcome in the failed flow.'})
                        save(repair_dir/'generated.json', repaired)
                        repaired['target'] = normalize_css(repaired['target'], frontend, repair_dir)
                        repaired['patches'] = make_patches(generated['target'], repaired['target'], item['task_type'])
                        materialize(repaired['target'])
                        reviewed = lenient_check(repaired['browser_check']) if args.acceptance=='lenient' else repaired['browser_check']
                        save(repair_dir/'original_check.json',failed_check)
                        save(failed_check_path,reviewed)
                        if failed_index==index:
                            check = reviewed
                        generated['target'] = repaired['target']
                        evidence = await verify_flow(check, repair_dir, index)
                        generated['patches'] += repaired['patches']
                        generated['attempts'] += repaired['attempts']
                        generated['generation_seconds'] += repaired['generation_seconds']
                        generated['target'] = repaired['target']
                        repair_history.append({'applied_patches':repaired['patches'],
                            'outcome':[{'subtask_index':c['subtask_index'],'status':c['status'],
                                'failed_steps':[s for s in c.get('steps',[]) if not s.get('ok')],
                                'console_errors':c.get('console_errors',[])} for c in evidence['checks']]})
                    if not evidence['checks'] or any(x['status'] != 'ok' for x in evidence['checks']):
                        raise ValueError('same browser flow failed after bounded repairs')
                before_edit = current
                current = generated['target']
                patches += generated['patches']
                save(step/'checkpoint.json', {'source_sha256': digest(original), 'target': current,
                    'patches': patches, 'browser_verified': evidence is not None})
                results.append({'index': index, 'task_type': item['task_type'], 'status': 'ok',
                    'generation_seconds': generated['generation_seconds'],
                    'elapsed_seconds': round(time.monotonic()-started, 3),
                    'attempts': generated['attempts'], 'browser_verified': evidence is not None})
                if evidence is not None and item['task_type'] in EDIT_SKILLS:
                    try:
                        results[-1]['rsi_candidate']=enqueue_skill_learning(output=output,
                            before=before_edit,after=current,skill=EDIT_SKILLS[item['task_type']],item=item,
                            evidence_ref=step/'checkpoint.json')
                    except Exception as exc:
                        results[-1]['rsi_note']=f'queue skipped: {type(exc).__name__}: {exc}'
        except Exception as exc:
            status=getattr(getattr(exc,'response',None),'status_code',None)
            system_failure=status in {401,403} or any(code in str(exc).lower()
                for code in ('insufficient_quota','invalid_api_key','arrea','quotaexhausted'))
            results.append({'index': index, 'task_type': item['task_type'], 'status': 'error',
                'failure_scope':'system' if system_failure else 'case',
                'error': f'{type(exc).__name__}: {exc}', 'elapsed_seconds': round(time.monotonic()-started,3)})
            materialize(current)
            break
        finally:
            calls = [json.loads(p.read_text()) for p in step.glob('**/call_*.json')]
            results[-1]['usage'] = {
                'calls':len(calls),
                'input_tokens':sum((c.get('usage') or {}).get('prompt_tokens',0) for c in calls),
                'output_tokens':sum((c.get('usage') or {}).get('completion_tokens',0) for c in calls),
                'cached_input_tokens':sum((c.get('usage') or {}).get('prompt_tokens_details',{}).get('cached_tokens',0) for c in calls),
                'unknown_usage_calls':sum(c.get('usage') is None for c in calls),
                'billed_cost':None,'cost_status':'provider did not return a verified billed amount'}
            results[-1]['issues'] = [json.loads(p.read_text()) for p in step.glob('**/browser_evidence.json')
                if any(c.get('status')!='ok' for c in json.loads(p.read_text()).get('checks',[]))]
            save(step/'result.json', results[-1])
            save(output/'worker_state.json', {'status':'between_steps','subtask_index':index,
                'deadline':time.time()+20})
            print(json.dumps({k:v for k,v in results[-1].items() if k not in {'issues','attempts'}}, ensure_ascii=False), flush=True)
    success = len(results)==len(selected) and all(x['status']=='ok' for x in results)
    replay = apply_patches(original, patches) == current
    result = {'instance_id': case.get('instance_id'), 'status': ('ok' if len(selected)==len(tasks) else 'partial') if success and replay else 'error',
        'provider_profile':'qwen' if qwen else 'njulink-degraded', 'model':config.generator_model, 'workers':1,
        'acceptance':args.acceptance,
        'unattended':unattended,'wall_seconds':round(time.monotonic()-run_started,3),
        'task_count':len(tasks), 'completed_steps':sum(x['status']=='ok' for x in results),
        'steps':results, 'patch_replay':replay, 'response':[{**p,'path':prefix+p['path']} for p in patches],
        'reference':{'dst_code':[{**x,'path':prefix+x['path']} for x in current]},
        'source_sha256':digest(original), 'target_sha256':digest(current)}
    save(output/'result.json', result)
    return result
