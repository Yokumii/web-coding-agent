#!/usr/bin/env python3
"""Isolated fast-Edit worker with an independent per-subtask process watchdog."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_g2_compound_edit import _read_case, normalize_input_case


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    tmp.replace(path)


def stop(process):
    if process is None:
        return
    # Kill the whole owned session, including children left after parent exit.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(args):
    parent = os.getppid()
    cancelled = False
    def cancel(*_):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frontend = output/'frontend'
    preview = worker = None
    started = time.time()
    case = normalize_input_case(_read_case(args.case))
    try:
        if (output/'input.json').exists():
            raise ValueError('worker output is not fresh; preserve it and use a new attempt directory')
        frontend.mkdir(exist_ok=True)
        code = case['source_code']
        prefix = 'code/' if all(x['path'].startswith('code/') for x in code) else ''
        for item in code:
            relative = Path(item['path'][len(prefix):])
            if relative.is_absolute() or '..' in relative.parts or 'node_modules' in relative.parts:
                raise ValueError('unsafe source path')
            path = frontend/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item['code'])
        dependencies = args.dependencies.resolve()
        vite = dependencies/'vite/bin/vite.js'
        if not vite.is_file():
            raise ValueError('dependencies must point to preinstalled node_modules containing Vite')
        (frontend/'node_modules').symlink_to(dependencies, target_is_directory=True)
        configs=[frontend/f'vite.config.{suffix}' for suffix in ('js','ts','mjs','mts','cjs','cts')]
        original=next((p for p in configs if p.is_file()),None)
        config=frontend/'.harness-vite.config.mjs'
        prelude=(f'import original from {json.dumps("./"+original.name)};\n' if original
                 else 'const original = {};\n')
        config.write_text(prelude+'export default async env => { const base = '
            'await (typeof original === "function" ? original(env) : original); '
            'return {...base, cacheDir:'+json.dumps(str(output/'vite-cache'))+'}; };\n')
        url = f'http://127.0.0.1:{args.port}'
        # Never attach this job to an unrelated preview occupying its port.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', args.port))
        with (output/'preview.log').open('a') as log:
            preview = subprocess.Popen(['node',str(vite),'--host','127.0.0.1','--port',str(args.port),
                '--strictPort','--config',str(config)], cwd=frontend, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time()+20
        local_http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            if cancelled or os.getppid()!=parent:
                raise InterruptedError('batch cancelled or supervisor lost')
            if preview.poll() is not None or time.time()>deadline:
                raise RuntimeError('preview startup failed; see preview.log')
            try:
                request=urllib.request.Request(url,headers={'Accept':'text/html'})
                with local_http.open(request, timeout=.5) as response:
                    if response.status==200:
                        break
            except OSError:
                time.sleep(.1)
        command = [sys.executable,'-u',str(ROOT/'scripts/run_g2_compound_edit.py'),
            '--case',str(args.case.resolve()),'--output',str(output),'--fast-gt','--unattended',
            '--provider-profile',args.provider_profile,'--model',args.model,
            '--acceptance','lenient','--browser-url',url,'--subtask-timeout',str(int(args.subtask_timeout))]
        with (output/'worker.log').open('a') as log:
            worker = subprocess.Popen(command, stdout=log,stderr=subprocess.STDOUT,start_new_session=True,
                env={**os.environ,'PYTHONPATH':str(ROOT)})
        deadline = time.time()+45
        while worker.poll() is None:
            if cancelled or os.getppid()!=parent:
                raise InterruptedError('batch cancelled or supervisor lost')
            state_path=output/'worker_state.json'
            state=json.loads(state_path.read_text()) if state_path.exists() else {}
            active_deadline=float(state.get('deadline',deadline))
            save(output/'heartbeat.json', {'time':time.time(),'pid':worker.pid,
                'subtask_index':state.get('subtask_index'),'deadline':active_deadline})
            if time.time()>=active_deadline:
                raise TimeoutError(f"process deadline exceeded at subtask {state.get('subtask_index')}")
            if preview.poll() is not None:
                raise RuntimeError('preview process exited')
            time.sleep(.2)
        if not (output/'result.json').exists():
            raise RuntimeError(f'worker exited {worker.returncode} without result')
        return json.loads((output/'result.json').read_text())
    except Exception as exc:
        stop(worker)
        worker=None
        steps=[json.loads(p.read_text()) for p in sorted(output.glob('step_*/result.json'))]
        calls=[json.loads(p.read_text()) for p in output.glob('step_*/**/call_*.json')]
        result={'instance_id':case.get('instance_id'),'status':'cancelled' if isinstance(exc,InterruptedError) else 'error',
            'error':f'{type(exc).__name__}: {exc}','wall_seconds':round(time.time()-started,3),
            'completed_steps':sum(s.get('status')=='ok' for s in steps),'steps':steps,
            'usage':{'calls':len(calls),'input_tokens':sum((c.get('usage') or {}).get('prompt_tokens',0) for c in calls),
                'output_tokens':sum((c.get('usage') or {}).get('completion_tokens',0) for c in calls),
                'unknown_usage_calls':sum(not c.get('usage') for c in calls),'billed_cost':None}}
        save(output/'result.json',result)
        return result
    finally:
        stop(worker)
        stop(preview)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependencies',type=Path,required=True)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--model',default='qwen3.7-max')
    parser.add_argument('--provider-profile',default='qwen',choices=['qwen','experimental-luna'])
    parser.add_argument('--subtask-timeout',type=float,default=180)
    args=parser.parse_args()
    if not 0<args.subtask_timeout<=180:
        parser.error('subtask timeout must be in (0,180]')
    result=run(args)
    print(json.dumps({k:v for k,v in result.items() if k not in {'steps','response','reference'}}),flush=True)
    sys.exit(0 if result['status']=='ok' else 2)
