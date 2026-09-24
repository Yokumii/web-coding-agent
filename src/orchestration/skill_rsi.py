"""Small, bounded Skill learning. Production Edit only writes a candidate packet."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

MAX_HELPERS = 3
MAX_CODE = 1800
CATALOG = 'learned.json'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)


def load_helpers(folder):
    path=folder/'references'/CATALOG
    if not path.exists():
        return []
    value=json.loads(path.read_text())
    helpers=value['helpers']
    if len(helpers)>MAX_HELPERS or len({h['name'] for h in helpers})!=len(helpers):
        raise ValueError('RSI catalog exceeds capacity')
    if value.get('revision')!=hashlib.sha256(json.dumps(helpers,sort_keys=True).encode()).hexdigest():
        raise ValueError('RSI catalog integrity mismatch')
    for helper in helpers:
        validate_helper(helper)
    return helpers


def validate_helper(helper):
    name,code=helper['name'],helper['code']
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,48}',name):
        raise ValueError('invalid helper name')
    if not code.startswith(f'function {name}(') or len(code)>MAX_CODE or len(code.splitlines())>60:
        raise ValueError('helper must be one compact named function')
    if len(helper['usage'])>240 or not 2<=len(helper['tests'])<=4 or len(json.dumps(helper['tests']))>2400:
        raise ValueError('helper needs short integration instructions and 2-4 tests')
    # This first version deliberately admits pure utilities only, not DOM/I/O engines.
    if re.search(r'\b(import|export|require|eval|Function|fetch|window|document|globalThis|localStorage|sessionStorage|process)\b',code):
        raise ValueError('only dependency-free pure functions are supported')
    for test in helper['tests']:
        if not isinstance(json.loads(test['args_json']),list):
            raise ValueError('helper test arguments must be an array')
        json.loads(test['expected_json'])


def helper_context(folder):
    try:
        helpers=load_helpers(folder)
    except (OSError,ValueError,KeyError,TypeError):
        return '',{}
    if not helpers:
        return '',{}
    instructions='\n## Reusable small helpers\nUse these existing functions when relevant; import/call them instead of reimplementing.\n'
    instructions+='\n'.join(f"- {h['name']}: {h['usage']}" for h in helpers)
    plain='\n\n'.join(h['code'] for h in helpers)+'\n'
    esm=plain+'export { '+', '.join(h['name'] for h in helpers)+' };\n'
    return instructions,{'learned.js':plain,'learned.mjs':esm}


def enqueue(*, output, before, after, skill, item, evidence_ref):
    """Best-effort disk write only: no model or validation on the Edit critical path."""
    original={x['path']:x['code'] for x in before}
    changed=[x for x in after if original.get(x['path'])!=x['code']
        and Path(x['path']).suffix in {'.js','.mjs','.jsx','.ts','.tsx'}
        and not x['path'].endswith(('learned.js','learned.mjs'))]
    if not changed:
        return None
    sources=[]
    budget=18000
    for x in sorted(changed,key=lambda x:('/edit-skills/' in x['path'],len(x['code']))):
        if budget<500:
            break
        snippet=x['code'][:min(budget,12000)]
        sources.append({'path':x['path'],'code':snippet,'truncated':len(snippet)!=len(x['code'])})
        budget-=len(snippet)
    packet={'skill':skill,'instruction':item,'source':sources,'evidence_ref':str(evidence_ref)}
    key=hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest()[:20]
    path=output/'rsi_queue'/f'{key}.json'
    save(path,packet)
    return str(path)


async def check_helpers(helpers):
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium
    async with asyncio.timeout(6):
        async with async_playwright() as playwright:
            browser=await launch_chromium(playwright,headless=True)
            try:
                page=await browser.new_page()
                await page.route('**/*',lambda route:route.abort())
                for helper in helpers:
                    for test in helper['tests']:
                        actual=await page.evaluate('''({code,name,args}) => {
                            const fn = new Function('"use strict";'+code+'; return '+name)();
                            return fn(...args);
                        }''',{'code':helper['code'],'name':helper['name'],'args':json.loads(test['args_json'])})
                        if actual!=json.loads(test['expected_json']):
                            raise ValueError(f"helper compatibility test failed: {helper['name']}")
            finally:
                await browser.close()


async def learn(packet_path, library, config, output):
    """One semantic extraction call; preserve old helpers and their behavioral tests."""
    from src.agents.openai_runner import OpenAIHTTPClient
    from src.orchestration.edit_skills import EDIT_SKILLS
    packet=json.loads(packet_path.read_text())
    skill=packet['skill']
    if skill not in EDIT_SKILLS.values():
        raise ValueError('unknown Skill')
    folder=library/skill
    existing=load_helpers(folder)
    original_bytes=(folder/'references'/CATALOG).read_bytes() if (folder/'references'/CATALOG).exists() else None
    string={'type':'string'}
    test={'type':'object','properties':{'args_json':string,'expected_json':string},
        'required':['args_json','expected_json'],'additionalProperties':False}
    schema={'type':'object','properties':{'action':{'type':'string','enum':['skip','upsert']},
        'reason':string,'name':string,'code':string,'usage':string,
        'tests':{'type':'array','items':test}},
        'required':['action','reason','name','code','usage','tests'],'additionalProperties':False}
    core={p.name:p.read_text() for p in (folder/'references').glob('*.mjs')}
    request={'accepted_edit':packet,'skill':(folder/'SKILL.md').read_text(),
        'existing_helpers':existing,'core':core}
    output.mkdir(parents=True,exist_ok=True)
    save(output/'request.json',request)
    metric={'status':'started','usage':None,'model':config.generator_model}
    started=time.monotonic()
    try:
        response=await OpenAIHTTPClient(config,20).complete(model=config.generator_model,max_tokens=1800,
            messages=[{'role':'system','content':'''Extract at most ONE genuinely useful, already exercised reusable pure JavaScript function
from this accepted Edit. Prefer retaining the original function body. Remove business names, labels,
field/DOM identifiers, storage keys and project-specific data assumptions; use arguments instead.
Inspect individual new functions, not only the feature as a whole. A pure mapping/calculation used
by a DOM component can be reusable even when the containing component is not. Such a helper does
not duplicate the engine merely because that engine consumes its result.
Standard UI modes and mathematical parameters are not business data. Compare actual existing code:
sharing a broad feature category is NOT duplication. A small helper supporting additional generic
options absent from the core is a valid extension; retain the engine and expose the helper separately.
Do not extract a whole component, clone an existing core API, invent new capabilities, or retain dead code.
Check existing helpers and core: merge the SAME capability under its existing name, never add an alias
or duplicate. Existing calls must remain compatible. If already covered, too trivial, unsafe, or uncertain,
SKIP. At most 3 helpers per Skill; skip a new capability when full. One named function declaration,
no imports, exports, external dependencies, DOM, storage, network, closure variables or side effects.
Maximum 60 lines and 1800 characters of code; usage <=240 characters, just signature and integration.
Provide 2-4 short JSON input/output tests, including edge cases; test JSON is data, never code.
Return the required schema; skip uses empty name/code/usage and empty tests. Reason one sentence.'''},
                {'role':'user','content':json.dumps(request,ensure_ascii=False)}],
            _stream=True,enable_thinking=False,
            response_format={'type':'json_schema','json_schema':{'name':'skill_helper','strict':True,'schema':schema}})
        metric.update(status='completed',usage=response.get('usage'))
        proposal=json.loads(response['choices'][0]['message']['content'])
        save(output/'proposal.json',proposal)
        if proposal['action']=='skip':
            return {'status':'skipped','reason':proposal['reason']}
        validate_helper(proposal)
        helper={k:proposal[k] for k in ('name','code','usage','tests')}
        previous=next((h for h in existing if h['name']==helper['name']),None)
        if previous:
            if helper['code']==previous['code']:
                return {'status':'skipped','reason':'already present'}
            # Retain old vectors for compatibility instead of letting the model delete them.
            await check_helpers([{**helper,'tests':previous['tests']}])
        elif len(existing)>=MAX_HELPERS:
            return {'status':'skipped','reason':'Skill helper capacity reached'}
        elif any(re.sub(r'\s+','',h['code']).replace(h['name'],'FN',1)==
                 re.sub(r'\s+','',helper['code']).replace(helper['name'],'FN',1) for h in existing):
            return {'status':'skipped','reason':'duplicate implementation'}
        helpers=[h for h in existing if h['name']!=helper['name']]+[helper]
        await check_helpers(helpers)
        if previous:
            helper['tests']=previous['tests']
        catalog=folder/'references'/CATALOG
        if (catalog.read_bytes() if catalog.exists() else None)!=original_bytes:
            raise ValueError('Skill changed concurrently; proposal retained without publishing')
        revision=hashlib.sha256(json.dumps(helpers,sort_keys=True).encode()).hexdigest()
        save(catalog,{'revision':revision,'helpers':helpers,'source':str(packet_path),
            'validation':str(output.resolve()),'evidence_ref':packet['evidence_ref']})
        return {'status':'published','skill':skill,'helper':helper['name'],'revision':revision}
    except BaseException as exc:
        metric.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        metric['elapsed_seconds']=round(time.monotonic()-started,3)
        save(output/'call_1.json',metric)
