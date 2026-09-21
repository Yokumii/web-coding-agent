#!/usr/bin/env python3
"""Bounded parallel launcher for frozen 0905 G2 compound Edit cases."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any


RUNNER = Path(__file__).resolve().with_name("run_g2_compound_edit.py")


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


def _run_one(args: argparse.Namespace, ordinal: int, job: dict[str, Any]) -> dict[str, Any]:
    case_id = str(job["instance_id"])
    case_path = Path(str(job["case"]))
    output = args.output / case_id
    existing = _latest_result(output)
    if existing and existing.get("status") in {"ok", "incomplete"}:
        return existing
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
        "--port",
        str(args.base_port + ordinal),
        "--budget-usd",
        str(args.budget_usd),
        "--request-timeout",
        str(args.request_timeout),
        "--case-timeout",
        str(args.case_timeout),
    ]
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(RUNNER.parent.parent)},
        )
        try:
            process.wait(timeout=args.case_timeout + 60)
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
            "status": "error",
            "instance_id": case_id,
            "error": f"worker exited {process.returncode} without result",
        }
        _save(output / "result.json", result)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    jobs = _select_jobs(plan, args.limit)
    args.output.mkdir(parents=True, exist_ok=True)
    selection = {
        "schema_version": "g2-compound-batch-v1",
        "source_plan": str(args.plan.resolve()),
        "model": args.model,
        "workers": args.workers,
        "case_timeout_seconds": args.case_timeout,
        "jobs": jobs,
    }
    selection_path = args.output / "selection.json"
    if selection_path.is_file() and json.loads(selection_path.read_text()) != selection:
        raise ValueError("batch selection/config changed; use a new output directory")
    _save(selection_path, selection)
    counts: dict[str, int] = {}
    total_cost = 0.0
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_one, args, ordinal, job): job
            for ordinal, job in enumerate(jobs)
        }
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            status = str(result.get("status") or "error")
            counts[status] = counts.get(status, 0) + 1
            total_cost += float(result.get("cost_usd") or 0.0)
            progress = {
                "status": "running" if completed < len(jobs) else "complete",
                "selected": len(jobs),
                "processed": completed,
                "counts": counts,
                "cost_usd": total_cost,
                "elapsed_seconds": round(time.time() - started, 3),
                "last_instance_id": result.get("instance_id"),
            }
            _save(args.output / "status.json", progress)
            print(json.dumps(progress, ensure_ascii=False), flush=True)
    return json.loads((args.output / "status.json").read_text())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--limit", type=int, default=20)
    result.add_argument("--workers", type=int, default=4)
    result.add_argument("--base-port", type=int, default=19000)
    result.add_argument("--model", default="gpt-5.6-luna")
    result.add_argument("--budget-usd", type=float, default=20.0)
    result.add_argument("--request-timeout", type=int, default=300)
    result.add_argument("--case-timeout", type=float, default=14400)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.workers < 1 or args.workers > 20:
        raise SystemExit("--workers must be between 1 and 20")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
