#!/usr/bin/env python3
"""Concurrent, resumable scheduler for independent Harness tasks.

The scheduler is deliberately provider-agnostic. It calls the current harness
directly, assigns one frontend port per case, appends every terminal result, and
supports text plus JSONL tasks with edit routes and local multimodal inputs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import HarnessConfig
from src.orchestration.harness import run_harness
from scripts.prepare_forward_edit_seed import prepare_seed


@dataclass(frozen=True)
class BatchTask:
    id: str
    prompt: str
    workdir: str = ""
    task_mode: str = "auto"
    inputs: tuple[Path, ...] = ()
    target_routes: tuple[str, ...] = ()
    seed_frontend: Path | None = None
    seed_evaluation: Path | None = None


def _slugify(value: str, max_len: int = 48) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip().lower()).strip("-.")
    return value[:max_len] or "task"


def _stable_id(prompt: str, index: int) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
    return f"{index:04d}-{digest}"


def _safe_workdir_name(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValueError(f"batch workdir must be one safe directory name: {value!r}")
    return value


def parse_input_file(path: Path) -> list[BatchTask]:
    path = path.resolve()
    tasks: list[BatchTask] = []
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict) or "prompt" not in payload:
            prompt = line
            tasks.append(BatchTask(id=_stable_id(prompt, index), prompt=prompt))
            continue
        prompt = str(payload["prompt"]).strip()
        if not prompt:
            raise ValueError(f"empty prompt on line {index + 1}")
        task_id = str(payload.get("id") or _stable_id(prompt, index))
        workdir = str(payload.get("workdir") or "")
        if workdir:
            _safe_workdir_name(workdir)
        task_mode = str(payload.get("task_mode") or "auto")
        if task_mode not in {"auto", "generate", "edit"}:
            raise ValueError(f"unsupported task_mode on line {index + 1}: {task_mode}")
        raw_inputs = payload.get("inputs") or []
        if not isinstance(raw_inputs, list) or not all(isinstance(item, str) for item in raw_inputs):
            raise ValueError(f"inputs must be a string list on line {index + 1}")
        raw_routes = payload.get("target_routes") or []
        if not isinstance(raw_routes, list) or not all(isinstance(item, str) for item in raw_routes):
            raise ValueError(f"target_routes must be a string list on line {index + 1}")
        seed_frontend = payload.get("seed_frontend")
        seed_evaluation = payload.get("seed_evaluation")
        if bool(seed_frontend) != bool(seed_evaluation):
            raise ValueError(
                f"seed_frontend and seed_evaluation must be supplied together on line {index + 1}"
            )
        if seed_frontend and task_mode != "edit":
            raise ValueError(f"seed_frontend requires task_mode=edit on line {index + 1}")
        tasks.append(BatchTask(
            id=task_id,
            prompt=prompt,
            workdir=workdir,
            task_mode=task_mode,
            inputs=tuple((path.parent / item).resolve() for item in raw_inputs),
            target_routes=tuple(raw_routes),
            seed_frontend=(path.parent / str(seed_frontend)).resolve() if seed_frontend else None,
            seed_evaluation=(path.parent / str(seed_evaluation)).resolve() if seed_evaluation else None,
        ))
    seen: set[str] = set()
    duplicates = sorted(task.id for task in tasks if task.id in seen or seen.add(task.id))
    if duplicates:
        raise ValueError("duplicate batch task ids: " + ", ".join(duplicates))
    return tasks


def load_latest_results(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_id = str(record.get("id") or "")
        if task_id:
            latest[task_id] = record
    return latest


def _read_state(workdir: Path) -> dict[str, Any]:
    path = workdir / ".harness" / "harness_state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


async def run_batch(
    tasks: list[BatchTask], *, output_dir: Path, results_path: Path,
    workers: int, base_port: int, timeout_seconds: float,
    config_overrides: dict[str, Any] | None = None,
    resume_harness: bool = False,
) -> list[dict[str, Any]]:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    completed = {
        task_id for task_id, record in load_latest_results(results_path).items()
        if record.get("status") == "ok"
    }
    pending = [(index, task) for index, task in enumerate(tasks) if task.id not in completed]
    semaphore = asyncio.Semaphore(workers)
    write_lock = asyncio.Lock()
    emitted: list[dict[str, Any]] = []

    async def append(record: dict[str, Any]) -> None:
        async with write_lock:
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            emitted.append(record)

    async def run_one(index: int, task: BatchTask) -> None:
        async with semaphore:
            name = task.workdir or f"{index:04d}_{_slugify(task.id)}"
            workdir = output_dir / _safe_workdir_name(name)
            config = HarnessConfig(
                **{**(config_overrides or {}), "frontend_port": base_port + index}
            )
            started = time.monotonic()
            record: dict[str, Any] = {
                "id": task.id,
                "prompt": task.prompt,
                "workdir": str(workdir.resolve()),
                "task_mode": task.task_mode,
                "frontend_port": config.frontend_port,
            }
            try:
                async def execute_case() -> None:
                    if task.seed_frontend is not None and not workdir.exists():
                        assert task.seed_evaluation is not None
                        await asyncio.to_thread(
                            prepare_seed,
                            task.seed_frontend,
                            workdir,
                            task.seed_evaluation,
                        )
                    await run_harness(
                        task.prompt, workdir, config,
                        resume=resume_harness,
                        keep_frontend=task.task_mode == "edit",
                        task_mode=task.task_mode,
                        input_paths=list(task.inputs),
                        target_routes=list(task.target_routes),
                    )

                await asyncio.wait_for(execute_case(), timeout=timeout_seconds)
                state = _read_state(workdir)
                completed_run = state.get("last_verdict") == "completed"
                record.update({
                    "status": "ok" if completed_run else "incomplete",
                    "cost_usd": sum((state.get("costs") or {}).values()),
                    "last_completed_phase": state.get("last_completed_phase"),
                    "last_verdict": state.get("last_verdict"),
                })
                if not completed_run:
                    record["error"] = (
                        "harness returned without a completed verdict; rerun with "
                        "--resume-harness to continue its checkpoint"
                    )
            except asyncio.TimeoutError:
                record.update({"status": "timeout", "error": f"case exceeded {timeout_seconds:g}s"})
            except Exception as exc:
                record.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            record["duration_s"] = round(time.monotonic() - started, 3)
            await append(record)

    await asyncio.gather(*(run_one(index, task) for index, task in pending))
    return sorted(emitted, key=lambda item: tasks.index(next(task for task in tasks if task.id == item["id"])))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run independent harness tasks concurrently")
    parser.add_argument("input", type=Path, help="JSONL or one-prompt-per-line input")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--base-port", type=int, default=6100)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--max-budget", type=float)
    parser.add_argument("--planner-model")
    parser.add_argument("--generator-model")
    parser.add_argument("--evaluator-model")
    parser.add_argument("--resume-harness", action="store_true")
    args = parser.parse_args()
    overrides = {
        key: value for key, value in {
            "max_rounds": args.max_rounds,
            "max_budget_usd": args.max_budget,
            "planner_model": args.planner_model,
            "generator_model": args.generator_model,
            "evaluator_model": args.evaluator_model,
        }.items() if value is not None
    }
    results = args.results or args.output_dir / "results.jsonl"
    records = asyncio.run(run_batch(
        parse_input_file(args.input), output_dir=args.output_dir.resolve(),
        results_path=results.resolve(), workers=args.workers,
        base_port=args.base_port, timeout_seconds=args.timeout_seconds,
        config_overrides=overrides, resume_harness=args.resume_harness,
    ))
    print(json.dumps({
        "emitted": len(records),
        "ok": sum(item["status"] == "ok" for item in records),
        "timeout": sum(item["status"] == "timeout" for item in records),
        "incomplete": sum(item["status"] == "incomplete" for item in records),
        "error": sum(item["status"] == "error" for item in records),
        "results": str(results.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
