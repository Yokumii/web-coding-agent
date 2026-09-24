import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import run_g2_compound_batch as batch
from scripts.run_fast_edit_worker import stop
from scripts import run_fast_edit_worker as guard


def arguments(tmp_path, jobs):
    plan=tmp_path/'plan.json'
    plan.write_text(json.dumps({'jobs':jobs}))
    return batch.parser().parse_args(['--plan',str(plan),'--output',str(tmp_path/'output'),
        '--workers','1','--limit','2'])


def test_failed_job_does_not_stop_next_job_and_unknown_cost_is_not_zero(tmp_path, monkeypatch):
    args=arguments(tmp_path,[{'instance_id':name,'case':'unused','task_count':7} for name in ['bad','good']])
    called=[]
    def worker(args, ordinal, job):
        called.append(job['instance_id'])
        if ordinal==0:
            raise ValueError('bad input')
        return {'instance_id':'good','status':'ok','steps':[{'usage':{'input_tokens':10,'output_tokens':2,'calls':1}}]}
    monkeypatch.setattr(batch,'_run_one',worker)
    result=batch.run(args)
    assert called==['bad','good']
    assert result['counts']=={'error':1,'ok':1}
    assert result['cost_usd'] is None
    assert result['usage']['input_tokens']==10
    assert json.loads((args.output/'bad/result.json').read_text())['status']=='error'


@pytest.mark.parametrize('names',[['../escape'],['duplicate','duplicate']])
def test_unsafe_or_duplicate_outputs_rejected(tmp_path,names):
    args=arguments(tmp_path,[{'instance_id':n,'case':'unused','task_count':7} for n in names])
    with pytest.raises(ValueError,match='unique safe'):
        batch.run(args)


def test_stop_cleans_owned_process_group():
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
    stop(process)
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid,0)


def test_independent_guard_kills_hung_worker_and_preview(tmp_path,monkeypatch):
    import argparse
    import socket
    import time
    scripts=tmp_path/'scripts'
    scripts.mkdir()
    (scripts/'run_g2_compound_edit.py').write_text('''import json,sys,time
from pathlib import Path
output=Path(sys.argv[sys.argv.index('--output')+1])
(output/'worker_state.json').write_text(json.dumps({'subtask_index':1,'deadline':time.time()+.4}))
time.sleep(60)
''')
    deps=tmp_path/'node_modules'
    vite=deps/'vite/bin/vite.js'
    vite.parent.mkdir(parents=True)
    vite.write_text("require('http').createServer((q,r)=>{r.end('ready')}).listen(Number(process.argv[process.argv.indexOf('--port')+1]),'127.0.0.1');")
    case=tmp_path/'case.json'
    case.write_text(json.dumps({'instance_id':'guard-test','source_code':[{'path':'index.html','code':'ready'}]}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    monkeypatch.setattr(guard,'ROOT',tmp_path)
    started=time.monotonic()
    result=guard.run(argparse.Namespace(case=case,output=tmp_path/'output',dependencies=deps,
        port=port,model='unused',provider_profile='qwen',subtask_timeout=180))
    assert result['status']=='error'
    assert 'TimeoutError' in result['error']
    assert time.monotonic()-started<10
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1',port))!=0
