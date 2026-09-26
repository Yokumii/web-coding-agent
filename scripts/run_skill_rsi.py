#!/usr/bin/env python3
"""One bounded offline Skill update; never changes an Edit result."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import sys
import signal
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.config import HarnessConfig
from src.orchestration.skill_rsi import learn,save,PROCESS_TIMEOUT


def run(args):
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    result={'status':'skipped'}
    try:
        # A single learner can publish to this library. Nonblocking lock: never hold up production.
        with (args.library/'.rsi.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                result={'status':'skipped','reason':'another learner owns the library'}
                return result
            qwen=args.provider_profile=='qwen'
            profile=({'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1'} if qwen else
                json.loads((Path.home()/'.config/webcoding/experimental-luna.json').read_text()))
            credential='qwen-dashscope-openai.key' if qwen else 'njulink-degraded.key'
            key=(Path.home()/'.config/webcoding/credentials'/credential).read_text().strip()
            config=HarnessConfig(agent_runtime='openai',openai_api_key=key,
                openai_base_url=profile['base_url'].rstrip('/'),openai_wire_api='chat' if qwen else 'responses',
                openai_extra_headers=profile.get('http_headers',{}),openai_stream_read_retries=0,
                generator_model=args.model or ('qwen3.7-max' if qwen else 'gpt-5.6-luna'))
            result=asyncio.run(learn(args.packet,args.library,config,output))
    except Exception as exc:
        result={'status':'skipped','reason':f'{type(exc).__name__}: {exc}'}
    finally:
        save(output/'result.json',result)
    return result


if __name__=='__main__':
    if os.getpgrp()==os.getpid():
        parent=os.getppid()
        def watchdog():
            deadline=time.monotonic()+PROCESS_TIMEOUT
            while time.monotonic()<deadline and os.getppid()==parent:
                time.sleep(.2)
            os.killpg(os.getpid(),signal.SIGKILL)
        threading.Thread(target=watchdog,daemon=True).start()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet',type=Path,required=True)
    parser.add_argument('--library',type=Path,default=ROOT/'.agents/skills')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--provider-profile',choices=['qwen','experimental-luna'],default='qwen')
    parser.add_argument('--model')
    args=parser.parse_args()
    print(json.dumps(run(args),ensure_ascii=False),flush=True)
