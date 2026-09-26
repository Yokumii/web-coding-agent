#!/usr/bin/env python3
"""Bounded parallel launcher for frozen 0905 G2 compound Edit cases."""
from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import threading
from typing import Any


RUNNER = Path(__file__).resolve().with_name("run_g2_compound_edit.py")
if str(RUNNER.parent.parent) not in sys.path:
    sys.path.insert(0,str(RUNNER.parent.parent))
from src.orchestration.pricing import estimate_cost_usd

CANCELLED = threading.Event()


def _runtime_hash(root):
    fingerprint=hashlib.sha256()
    paths=sorted([*root.glob('src/**/*'),*root.glob('scripts/**/*'),*root.glob('.agents/skills/**/*')])
    for path in paths:
        if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc' and path.name!='.rsi.lock':
            fingerprint.update(str(path.relative_to(root)).encode())
            fingerprint.update(path.read_bytes())
    return fingerprint.hexdigest()


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _select_jobs(plan: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    jobs = [item for item in plan.get("jobs") or [] if isinstance(item, dict)]
    if not jobs:
        raise ValueError("plan has no jobs")
    if limit <= 0:
        return jobs
    by_count: dict[int, list[dict[str, Any]]] = {}
    for job in jobs:
        by_count.setdefault(int(job.get("task_count") or 0), []).append(job)
    selected: list[dict[str, Any]] = []
    counts = sorted(count for count in by_count if count)
    while len(selected) < limit and any(by_count.get(count) for count in counts):
        for count in counts:
            if by_count.get(count) and len(selected) < limit:
                selected.append(by_count[count].pop(0))
    return selected


def _latest_result(output: Path) -> dict[str, Any] | None:
    path = output / "result.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _response_stream_usage(path: Path) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "unknown_usage_calls": 0}
    if not path.is_file():
        return usage
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "request_completed":
            continue
        item = event.get("usage") or {}
        usage["calls"] += 1
        if not item:
            usage["unknown_usage_calls"] += 1
            continue
        usage["input_tokens"] += int(item.get("input_tokens", item.get("prompt_tokens", 0)) or 0)
        usage["output_tokens"] += int(item.get("output_tokens", item.get("completion_tokens", 0)) or 0)
    return usage


def _run_one(args: argparse.Namespace, ordinal: int, job: dict[str, Any]) -> dict[str, Any]:
    case_id = str(job["instance_id"])
    case_path = Path(str(job["case"]))
    if args.fast_gt:
        case_path=args.case_paths[case_id]
    elif not case_path.is_file() and getattr(args, "plan", None):
        portable_case = args.plan.parent / "cases" / case_path.name
        if portable_case.is_file():
            case_path = portable_case
    output = args.output / case_id
    existing = _latest_result(output)
    if existing and (args.fast_gt or existing.get("status") in {"ok", "incomplete"}):
        if any(s.get('failure_scope')=='system' for s in existing.get('steps',[])):
            CANCELLED.set()
        return existing
    if CANCELLED.is_set():
        return {'instance_id':case_id,'status':'cancelled'}
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "worker.log"
    command = [
        sys.executable,
        "-u",
        str(RUNNER),
        "--case",
        str(case_path),
        "--output",
        str(output),
        "--model",
        args.model,
        "--review-model",
        getattr(args, "review_model", args.model),
        "--port",
        str(args.base_port + ordinal),
        "--budget-usd",
        str(args.budget_usd),
        "--request-timeout",
        str(args.request_timeout),
        "--case-timeout",
        str(args.case_timeout),
        "--debug-max-rounds",
        str(getattr(args, "debug_max_rounds", 8)),
    ]
    if args.provider_profile:
        command.extend(["--provider-profile", args.provider_profile])
    if getattr(args, "production_atomic_contract", False):
        command.append("--production-atomic-contract")
    if args.fast_gt:
        command=[sys.executable,'-u',str(args.runtime_root/'scripts/run_fast_edit_worker.py'),
            '--case',str(case_path.resolve()),'--output',str(output.resolve()),
            '--dependencies',str(args.dependencies.resolve()),'--port',str(args.base_port+ordinal),
            '--model',args.model,'--provider-profile',args.provider_profile or 'qwen',
            '--case-timeout',str(args.case_timeout)]
        if args.reverse_validate_root:
            command.extend(['--reverse-validate-root',str(args.reverse_validate_root.resolve()),
                '--reverse-node',args.reverse_node,'--reverse-playwright',args.reverse_playwright,
                '--reverse-chromium',args.reverse_chromium])
    elif getattr(args, 'reverse_validate_root', None):
        command.extend(['--reverse-validate-root',str(args.reverse_validate_root.resolve()),
            '--reverse-node',getattr(args, 'reverse_node', 'node')])
        if getattr(args, 'reverse_playwright', None):
            command.extend(['--reverse-playwright',args.reverse_playwright])
        if getattr(args, 'reverse_chromium', None):
            command.extend(['--reverse-chromium',args.reverse_chromium])
        if getattr(args, 'reverse_extra_node_modules', None):
            command.extend(['--reverse-extra-node-modules',args.reverse_extra_node_modules])
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(RUNNER.parent.parent)},
        )
        try:
            deadline = time.monotonic() + args.case_timeout + 60
            while process.poll() is None:
                if CANCELLED.wait(.5):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, args.case_timeout + 60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            result = {
                "status": "error",
                "instance_id": case_id,
                "error": "case process hard timeout",
            }
            _save(output / "result.json", result)
            return result
    result = _latest_result(output)
    if result is None:
        result = {
            "status": "cancelled" if CANCELLED.is_set() else "error",
            "instance_id": case_id,
            "error": (
                "batch cancelled"
                if CANCELLED.is_set()
                else f"worker exited {process.returncode} without result"
            ),
        }
        _save(output / "result.json", result)
    usage = _response_stream_usage(output / "response_stream.jsonl")
    if usage["calls"]:
        result["usage"] = usage
        result["cost_usd"] = estimate_cost_usd(args.model, usage)
        _save(output / "result.json", result)
    if args.fast_gt:
        errors=' '.join(str(x.get('error','')) for x in [result,*result.get('steps',[])])
        if any(s.get('failure_scope')=='system' for s in result.get('steps',[])) or any(
                code in errors for code in ('insufficient_quota','invalid_api_key','401 from','403 from')):
            CANCELLED.set()
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    CANCELLED.clear()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: CANCELLED.set())
        signal.signal(signal.SIGINT, lambda *_: CANCELLED.set())
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    jobs = _select_jobs(plan, args.limit)
    identifiers=[str(j['instance_id']) for j in jobs]
    if len(set(identifiers))!=len(identifiers) or any(
            not s or s in {'.','..'} or Path(s).name!=s or '/' in s or '\\' in s for s in identifiers):
        raise ValueError('job identifiers must be unique safe directory names')
    if not jobs or args.base_port<1024 or args.base_port+len(jobs)>65536:
        raise ValueError('empty selection or invalid port range')
    if args.fast_gt and (not args.dependencies or not 0<args.case_timeout<=2400 or args.limit<=0):
        raise ValueError('fast GT requires --dependencies and a case timeout <=2400')
    args.output.mkdir(parents=True, exist_ok=True)
    selection = {
        "schema_version": "g2-compound-batch-v1",
        "source_plan": str(args.plan.resolve()),
        "model": args.model,
        "review_model": getattr(args, "review_model", args.model),
        "provider_profile": args.provider_profile,
        "production_atomic_contract": getattr(args, "production_atomic_contract", False),
        "workers": args.workers,
        "case_timeout_seconds": args.case_timeout,
        "jobs": jobs,
    }
    if args.fast_gt:
        root=RUNNER.parent.parent
        selection.update(fast_gt=True,case_timeout_seconds=args.case_timeout,
            dependencies=str(args.dependencies.resolve()),code_skill_sha256=_runtime_hash(
                args.output/'runtime' if (args.output/'runtime').is_dir() else root),
            input_hashes={str(j['case']):hashlib.sha256(Path(j['case']).read_bytes()).hexdigest() for j in jobs})
    selection_path = args.output / "selection.json"
    if selection_path.is_file() and json.loads(selection_path.read_text()) != selection:
        raise ValueError("batch selection/config changed; use a new output directory")
    _save(selection_path, selection)
    if args.fast_gt:
        args.runtime_root=args.output.resolve()/'runtime'
        if not args.runtime_root.exists():
            staging=args.output.resolve()/'runtime_staging'
            if staging.exists():
                raise ValueError('incomplete runtime snapshot; preserve output and choose a fresh batch directory')
            for directory in ('src','scripts','.agents/skills'):
                shutil.copytree(root/directory,staging/directory,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            staging.rename(args.runtime_root)
        if _runtime_hash(args.runtime_root)!=selection['code_skill_sha256']:
            raise ValueError('runtime snapshot changed; no jobs dispatched')
        args.case_paths={}
        inputs=args.output.resolve()/'inputs'
        inputs.mkdir(exist_ok=True)
        for job in jobs:
            source=Path(job['case'])
            target=inputs/(job['instance_id']+('.json.gz' if source.suffix=='.gz' else '.json'))
            if not target.exists():
                shutil.copyfile(source,target)
            if hashlib.sha256(target.read_bytes()).hexdigest()!=selection['input_hashes'][str(job['case'])]:
                raise ValueError('input snapshot changed; no jobs dispatched')
            args.case_paths[job['instance_id']]=target
    counts: dict[str, int] = {}
    total_cost = 0.0
    unknown_cost = 0
    tokens = {'input_tokens':0,'output_tokens':0,'calls':0,'unknown_usage_calls':0}
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_one, args, ordinal, job): job
            for ordinal, job in enumerate(jobs)
        }
        for completed, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as exc:
                result={'status':'error','instance_id':futures[future]['instance_id'],
                    'error':f'{type(exc).__name__}: {exc}'}
                _save(args.output/result['instance_id']/'result.json',result)
            status = str(result.get("status") or "error")
            counts[status] = counts.get(status, 0) + 1
            total_cost += float(result.get("cost_usd") or 0.0)
            unknown_cost += result.get('cost_usd') is None
            usage=result.get('usage') or {}
            if not usage:
                for step in result.get('steps',[]):
                    for key in tokens:
                        usage[key]=usage.get(key,0)+(step.get('usage') or {}).get(key,0)
            for key in tokens:
                tokens[key]+=usage.get(key,0)
            progress = {
                "status": "running" if completed < len(jobs) else "complete",
                "selected": len(jobs),
                "processed": completed,
                "counts": counts,
                "cost_usd": None if unknown_cost else total_cost,
                "known_cost_usd":total_cost,"unknown_cost_tasks":unknown_cost,"usage":tokens.copy(),
                "elapsed_seconds": round(time.time() - started, 3),
                "last_instance_id": result.get("instance_id"),
            }
            _save(args.output / "status.json", progress)
            print(json.dumps(progress, ensure_ascii=False), flush=True)
    progress=json.loads((args.output / 'status.json').read_text())
    try:
        return _post_batch_rsi(args,progress)
    except Exception as exc:
        progress['rsi']={'status':'skipped','reason':f'{type(exc).__name__}: {exc}'}
        try:
            _save(args.output/'status.json',progress)
        except OSError:
            pass
        return progress


def _post_batch_rsi(args, progress):
    # Learning is outside all Edit deadlines and cannot turn a completed batch into failure.
    if args.fast_gt and args.rsi and not CANCELLED.is_set():
        from src.orchestration.skill_rsi import PROCESS_TIMEOUT
        packets=sorted(args.output.glob('*/rsi_queue/*.json'))
        learning=args.output/'rsi'
        if packets and not (learning/'result.json').exists():
            learning.mkdir(exist_ok=True)
            runtime=getattr(args,'runtime_root',RUNNER.parent.parent)
            from scripts.run_fast_edit_worker import stop
            # At most two distinct Skills evolve after one batch. This keeps RSI bounded while
            # avoiding the old behavior where only the lexicographically first packet could learn.
            selected=_select_rsi_packets(packets)
            results=[]
            for skill,packet in selected.items():
                destination=learning/skill
                destination.mkdir(exist_ok=True)
                result_path=destination/'result.json'
                if not result_path.exists() and not CANCELLED.is_set():
                    command=[sys.executable,str(runtime/'scripts/run_skill_rsi.py'),
                        '--packet',str(packet.resolve()),'--output',str(destination.resolve()),
                        '--library',str(RUNNER.parent.parent/'.agents/skills'),'--model',args.model,
                        '--provider-profile',args.provider_profile or 'qwen']
                    process=None
                    try:
                        with (destination/'worker.log').open('a') as log:
                            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                        deadline=time.monotonic()+PROCESS_TIMEOUT
                        while process.poll() is None and not CANCELLED.wait(.2):
                            if time.monotonic()>=deadline:
                                break
                    except Exception as exc:
                        _save(result_path,{'status':'skipped','skill':skill,'reason':str(exc)})
                    finally:
                        stop(process)
                    if not result_path.exists():
                        _save(result_path,{'status':'skipped','skill':skill,
                            'reason':'learner timeout or cancellation'})
                if result_path.exists():
                    results.append(json.loads(result_path.read_text()))
            _save(learning/'result.json',{'status':'complete','attempted':len(results),'results':results})
        if (learning/'result.json').exists():
            progress['rsi']=json.loads((learning/'result.json').read_text())
            progress['rsi_usage']=[json.loads(metric.read_text())
                for metric in sorted(learning.glob('*/call_1.json'))]
            _save(args.output/'status.json',progress)
    return progress


def _select_rsi_packets(packets, limit=2):
    selected={}
    for packet in packets:
        try:
            skill=json.loads(packet.read_text())['skill']
        except (OSError,KeyError,TypeError,json.JSONDecodeError):
            continue
        selected.setdefault(skill,packet)
        if len(selected)>=limit:
            break
    return selected


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--limit", type=int, default=20)
    result.add_argument("--workers", type=int, default=4)
    result.add_argument("--base-port", type=int, default=19000)
    result.add_argument("--model", default="gpt-5.6-luna")
    result.add_argument("--review-model", default="gpt-5.6-luna")
    result.add_argument("--provider-profile", choices=["experimental-luna","njulink-degraded","openai","qwen"])
    result.add_argument('--fast-gt',action='store_true')
    result.add_argument('--no-rsi',dest='rsi',action='store_false',default=True,
        help='Disable bounded post-batch Skill evolution (up to two distinct Skills)')
    result.add_argument('--dependencies',type=Path)
    result.add_argument('--subtask-timeout',type=float,help=argparse.SUPPRESS)
    result.add_argument("--budget-usd", type=float, default=20.0)
    result.add_argument("--request-timeout", type=int, default=300)
    result.add_argument("--case-timeout", type=float, default=2400)
    result.add_argument(
        "--debug-max-rounds",
        type=int,
        choices=range(1, 11),
        default=10,
        help="Maximum Repair rounds after the initial Generate (default: 10)",
    )
    result.add_argument('--reverse-validate-root',type=Path)
    result.add_argument('--reverse-node',default='node')
    result.add_argument('--reverse-playwright')
    result.add_argument('--reverse-chromium')
    result.add_argument('--reverse-extra-node-modules')
    result.add_argument('--production-atomic-contract', action='store_true')
    return result


def main() -> int:
    args = parser().parse_args()
    if args.workers < 1 or args.workers > 20:
        raise SystemExit("--workers must be between 1 and 20")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
