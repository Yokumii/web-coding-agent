#!/usr/bin/env python3
"""Supervise an explicitly bounded batch with resource checks and a renewable monitor lease."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lease',type=Path,required=True)
    p.add_argument('--heartbeat',type=Path,required=True)
    p.add_argument('--lease-seconds',type=float,default=180)
    p.add_argument('--renew-for-pid',type=int,help='Maintain a lease only while this existing batch process lives')
    p.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args()
    if a.renew_for_pid:
        stat=Path(f'/proc/{a.renew_for_pid}/stat')
        def identity():
            try:
                fields=stat.read_text().rsplit(')',1)[1].split()
                return fields[19] if fields[0]!='Z' else None
            except OSError: return None
        original=identity()
        if original is None: return 2
        while identity()==original:
            a.lease.touch()
            time.sleep(15)
        return 0
    command=a.command[1:] if a.command[:1]==['--'] else a.command
    if not command: raise ValueError('missing command')
    cancelled=[]
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,lambda s,_:cancelled.append(f'signal {s}'))
    proc=None
    try:
        while True:
            mem={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
            load=os.getloadavg()
            if not a.lease.exists() or time.time()-a.lease.stat().st_mtime>a.lease_seconds:
                cancelled.append('monitor lease expired')
            if mem['MemAvailable']<8*1024*1024 or load[1]>max(16,(os.cpu_count() or 1)*.85):
                cancelled.append('shared host resource guard')
            status={'time':time.time(),'load':load,'available_memory_kb':mem['MemAvailable'],
                'pid':proc.pid if proc else None,'stop_reason':cancelled}
            temp=a.heartbeat.with_suffix('.tmp');temp.write_text(json.dumps(status));temp.replace(a.heartbeat)
            if cancelled: break
            if proc is None: proc=subprocess.Popen(command,start_new_session=True)
            if proc.poll() is not None: return proc.returncode
            time.sleep(5)
        return 2
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        if cancelled: print(json.dumps({'supervisor_stop':cancelled}),flush=True)


if __name__=='__main__': raise SystemExit(main())
