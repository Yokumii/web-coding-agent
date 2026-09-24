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
from src.orchestration.skill_rsi import learn,save


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
            key=(Path.home()/'.config/webcoding/credentials/qwen-dashscope-openai.key').read_text().strip()
            config=HarnessConfig(agent_runtime='openai',openai_api_key=key,
                openai_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',openai_wire_api='chat',
                openai_stream_read_retries=0,generator_model=args.model)
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
            deadline=time.monotonic()+28
            while time.monotonic()<deadline and os.getppid()==parent:
                time.sleep(.2)
            os.killpg(os.getpid(),signal.SIGKILL)
        threading.Thread(target=watchdog,daemon=True).start()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet',type=Path,required=True)
    parser.add_argument('--library',type=Path,default=ROOT/'.agents/skills')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',default='qwen3.7-max')
    args=parser.parse_args()
    print(json.dumps(run(args),ensure_ascii=False),flush=True)
