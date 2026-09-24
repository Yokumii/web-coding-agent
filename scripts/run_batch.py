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
import subprocess
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
from src.utils.llm_json import extract_json_object


@dataclass(frozen=True)
class EditStep:
    id: str
    prompt: str
    inputs: tuple[Path, ...] = ()
    target_routes: tuple[str, ...] = ()
    atomic_plan: dict[str, Any] | None = None
    hidden_oracle_checks: tuple[dict[str, Any], ...] = ()
    chain_metadata: dict[str, Any] | None = None


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
    atomic_plan: dict[str, Any] | None = None
    hidden_oracle_checks: tuple[dict[str, Any], ...] = ()
    edits: tuple[EditStep, ...] = ()
    sequence_metadata: dict[str, Any] | None = None


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


def _parse_edit_steps(
    raw_edits: Any, *, source_path: Path, line_number: int
) -> tuple[EditStep, ...]:
    if not isinstance(raw_edits, list) or not raw_edits:
        raise ValueError(f"edits must be a non-empty object list on line {line_number}")
    steps: list[EditStep] = []
    for index, raw in enumerate(raw_edits, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"edit {index} must be an object on line {line_number}")
        prompt = str(raw.get("prompt") or raw.get("instruction") or "").strip()
        if not prompt:
            raise ValueError(f"edit {index} has no instruction on line {line_number}")
        raw_inputs = raw.get("inputs") or []
        raw_routes = raw.get("target_routes") or []
        hidden = raw.get("hidden_oracle_checks") or []
        if not isinstance(raw_inputs, list) or not all(isinstance(item, str) for item in raw_inputs):
            raise ValueError(f"edit {index} inputs must be strings on line {line_number}")
        if not isinstance(raw_routes, list) or not all(isinstance(item, str) for item in raw_routes):
            raise ValueError(f"edit {index} target_routes must be strings on line {line_number}")
        if not isinstance(hidden, list) or not all(isinstance(item, dict) for item in hidden):
            raise ValueError(f"edit {index} hidden_oracle_checks must be objects")
        atomic_plan = raw.get("atomic_plan")
        if atomic_plan is not None and not isinstance(atomic_plan, dict):
            raise ValueError(f"edit {index} atomic_plan must be an object")
        steps.append(EditStep(
            id=str(raw.get("id") or raw.get("edit_id") or f"q{index}"),
            prompt=prompt,
            inputs=tuple((source_path.parent / item).resolve() for item in raw_inputs),
            target_routes=tuple(raw_routes),
            atomic_plan=atomic_plan,
            hidden_oracle_checks=tuple(hidden),
            chain_metadata=dict(raw),
        ))
    if len({step.id for step in steps}) != len(steps):
        raise ValueError(f"edit ids must be unique on line {line_number}")
    prior: dict[str, dict[str, Any]] = {}
    for index, step in enumerate(steps, start=1):
        metadata = step.chain_metadata or {}
        for key, expected in (("edit_index", index), ("source_version", f"s{index - 1}"),
                              ("target_version", f"s{index}")):
            if key in metadata and metadata[key] != expected:
                raise ValueError(f"{step.id}: invalid linear {key}")
        dependencies = metadata.get("depends_on", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dep, str) or dep not in prior for dep in dependencies
        ):
            raise ValueError(f"{step.id}: dependencies must reference preceding edits")
        for key in ("requires", "produces", "acceptance", "preserve"):
            if key in metadata and not isinstance(metadata[key], list):
                raise ValueError(f"{step.id}: {key} must be a list")
        for requirement in metadata.get("requires", []):
            if not isinstance(requirement, dict):
                raise ValueError(f"{step.id}: requires entries must be objects")
            producer = requirement.get("source")
            if producer == "seed":
                continue
            if producer not in dependencies or requirement.get("state") not in {
                item.get("state") for item in prior[producer].get("produces", [])
                if isinstance(item, dict)
            }:
                raise ValueError(f"{step.id}: required state has no declared preceding producer")
        prior[step.id] = metadata
    return tuple(steps)


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
        if not isinstance(payload, dict):
            prompt = line
            tasks.append(BatchTask(id=_stable_id(prompt, index), prompt=prompt))
            continue
        raw_edits = payload.get("edits")
        sequence_metadata = dict(payload) if raw_edits is not None else None
        edit_source_path = path
        edit_sequence = payload.get("edit_sequence")
        if edit_sequence:
            sequence_path = (path.parent / str(edit_sequence)).resolve()
            sequence_payload = json.loads(sequence_path.read_text(encoding="utf-8"))
            raw_edits = sequence_payload.get("edits")
            sequence_metadata = sequence_payload
            edit_source_path = sequence_path
        edits = (
            _parse_edit_steps(
                raw_edits, source_path=edit_source_path, line_number=index + 1
            )
            if raw_edits is not None
            else ()
        )
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt and not edits:
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
        if edits and (task_mode != "edit" or not seed_frontend):
            raise ValueError(
                f"an Edit chain requires task_mode=edit and a verified seed pair on line {index + 1}"
            )
        atomic_plan = payload.get("atomic_plan")
        hidden = payload.get("hidden_oracle_checks") or []
        if atomic_plan is not None and not isinstance(atomic_plan, dict):
            raise ValueError(f"atomic_plan must be an object on line {index + 1}")
        if not isinstance(hidden, list) or not all(isinstance(item, dict) for item in hidden):
            raise ValueError(f"hidden_oracle_checks must be objects on line {index + 1}")
        tasks.append(BatchTask(
            id=task_id,
            prompt=prompt,
            workdir=workdir,
            task_mode=task_mode,
            inputs=tuple((path.parent / item).resolve() for item in raw_inputs),
            target_routes=tuple(raw_routes),
            seed_frontend=(path.parent / str(seed_frontend)).resolve() if seed_frontend else None,
            seed_evaluation=(path.parent / str(seed_evaluation)).resolve() if seed_evaluation else None,
            atomic_plan=atomic_plan,
            hidden_oracle_checks=tuple(hidden),
            edits=edits,
            sequence_metadata=sequence_metadata,
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


def _git_output(frontend: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=frontend, text=True, check=True, capture_output=True
    ).stdout


def _latest_grade_path(workdir: Path) -> Path:
    candidates = sorted(
        (workdir / ".harness").glob("grade_round_*.json"),
        key=lambda item: int(re.search(r"(\d+)$", item.stem).group(1)),
    )
    if not candidates:
        raise ValueError(f"accepted Edit has no grade artifact: {workdir}")
    return candidates[-1]


def _accepted_checks(workdir: Path, *, namespace: str) -> list[dict[str, Any]]:
    plan = json.loads(
        (workdir / ".harness" / "ui_verification_plan.json").read_text(encoding="utf-8")
    )
    checks = [
        json.loads(json.dumps(check))
        for sprint in plan.get("sprints") or []
        if isinstance(sprint, dict)
        for check in sprint.get("checks") or []
        if isinstance(check, dict)
    ]
    for index, check in enumerate(checks, start=1):
        original = str(check.get("id") or f"check-{index}")
        check["id"] = f"{namespace}__{original}"
        requirement = str(check.get("requirement_id") or "")
        if requirement:
            check["requirement_id"] = f"{namespace}__{requirement}"
    return checks


def _recover_failed_atomic_plan(workdir: Path, *, instruction: str) -> dict[str, Any] | None:
    """Reuse a paid Planner response after local validation failed.

    The artifact is reusable only when the append-only trace proves that its
    latest atomic-planner request contains this exact Edit instruction. Schema,
    grounding, and action validation still run again inside ``run_harness``.
    """
    plan_path = workdir / ".harness" / "atomic_edit_plan.json"
    trace_path = workdir / ".harness" / "traces" / "planner.jsonl"
    if not trace_path.is_file():
        return None
    expected = "Create the atomic Edit plan for this instruction:\n\n" + instruction.strip() + "\n\n"
    matching_run = False
    traced_payloads: list[dict[str, Any]] = []
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "run_start":
                matching_run = (
                    event.get("phase") == "atomic_edit_planner"
                    and str(event.get("prompt") or "").startswith(expected)
                )
                continue
            if (
                matching_run
                and event.get("event") == "assistant_response"
                and isinstance(event.get("content"), str)
            ):
                candidate = extract_json_object(str(event["content"]))
                if isinstance(candidate, dict):
                    traced_payloads.append(candidate)
        payload = (
            json.loads(plan_path.read_text(encoding="utf-8"))
            if plan_path.is_file()
            else None
        )
    except (OSError, ValueError, TypeError):
        return None
    if not traced_payloads and not matching_run:
        return None
    candidates = [*reversed(traced_payloads)]
    if isinstance(payload, dict):
        candidates.append(payload)
    for candidate in candidates:
        actions = [
            action
            for check in candidate.get("checks") or []
            if isinstance(check, dict)
            for action in check.get("actions") or []
            if isinstance(action, dict)
        ]
        if all(
            action.get("action") != "drag_and_drop"
            or (
                str(action.get("source_selector") or action.get("selector") or "").strip()
                and (
                    str(action.get("target_selector") or "").strip()
                    or re.fullmatch(
                        r".+(?::first-child|:nth-child\(\d+\))",
                        str(
                            action.get("source_selector")
                            or action.get("selector")
                            or ""
                        ).strip(),
                    )
                )
            )
            for action in actions
        ):
            return candidate
    return None


def _export_edit_ground_truth(workdir: Path, *, edit_id: str) -> dict[str, Any]:
    frontend = workdir / "frontend"
    seed = json.loads((workdir / "seed_manifest.json").read_text(encoding="utf-8"))
    source_commit = str(seed["baseline_commit"])
    target_commit = _git_output(frontend, "rev-parse", "HEAD").strip()
    patch = _git_output(frontend, "diff", "--binary", source_commit, target_commit)
    if not patch.strip():
        raise ValueError(f"accepted Edit {edit_id} produced an empty source-target diff")
    patch_path = workdir / "ground_truth.patch"
    patch_path.write_text(patch, encoding="utf-8")
    changed_files = [
        item for item in _git_output(
            frontend, "diff", "--name-only", source_commit, target_commit
        ).splitlines() if item
    ]
    return {
        "schema_version": "sequential-edit-ground-truth-v1",
        "edit_id": edit_id,
        "source_commit": source_commit,
        "target_commit": target_commit,
        "source_tree": _git_output(frontend, "rev-parse", f"{source_commit}^{{tree}}").strip(),
        "target_tree": _git_output(frontend, "rev-parse", f"{target_commit}^{{tree}}").strip(),
        "changed_files": changed_files,
        "patch": str(patch_path.resolve()),
    }


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
    latest_results = load_latest_results(results_path)
    for task in tasks:
        previous = latest_results.get(task.id, {})
        if task.edits and previous.get("status") == "ok" and (
            previous.get("sequence_metadata") != task.sequence_metadata
        ):
            raise ValueError(f"{task.id}: completed chain metadata changed; use a new run ID")
    completed = {
        task.id for task in tasks
        if (record := latest_results.get(task.id, {})).get("status") == "ok"
        and (not task.edits or [step.get("edit_id") for step in record.get("steps", [])]
             == [step.id for step in task.edits])
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
                **({"sequence_metadata": task.sequence_metadata} if task.edits else {}),
            }
            try:
                async def execute_case() -> None:
                    if task.edits:
                        assert task.seed_frontend is not None
                        assert task.seed_evaluation is not None
                        workdir.mkdir(parents=True, exist_ok=True)
                        source_frontend = task.seed_frontend
                        source_evaluation = task.seed_evaluation
                        prior_checks: list[list[dict[str, Any]]] = []
                        step_records: list[dict[str, Any]] = []
                        for step_index, step in enumerate(task.edits, start=1):
                            step_name = f"{step_index:02d}_{_slugify(step.id)}"
                            step_workdir = workdir / "steps" / step_name
                            existing_step_state = _read_state(step_workdir)
                            contract_path = step_workdir / ".harness" / "edit_task_contract.json"
                            if existing_step_state and contract_path.is_file():
                                saved = json.loads(contract_path.read_text(encoding="utf-8"))
                                if saved.get("chain_metadata") != step.chain_metadata:
                                    raise ValueError(f"{step.id}: chain metadata changed; use a new run directory")
                            recovered_atomic_plan = (
                                None
                                if existing_step_state or step.atomic_plan is not None
                                else _recover_failed_atomic_plan(
                                    step_workdir, instruction=step.prompt
                                )
                            )
                            if not step_workdir.exists():
                                await asyncio.to_thread(
                                    prepare_seed,
                                    source_frontend,
                                    step_workdir,
                                    source_evaluation,
                                )
                            if existing_step_state.get("last_verdict") != "completed":
                                if existing_step_state and not resume_harness:
                                    raise ValueError(
                                        f"Edit step {step.id} is incomplete; rerun with --resume-harness"
                                    )
                                await run_harness(
                                    step.prompt,
                                    step_workdir,
                                    config,
                                    resume=bool(existing_step_state) and resume_harness,
                                    keep_frontend=True,
                                    task_mode="edit",
                                    input_paths=list(step.inputs),
                                    target_routes=list(step.target_routes or ("/",)),
                                    atomic_plan=(
                                        None
                                        if existing_step_state
                                        else (step.atomic_plan or recovered_atomic_plan)
                                    ),
                                    hidden_oracle_checks=(
                                        None if existing_step_state else list(step.hidden_oracle_checks)
                                    ),
                                    prior_accepted_checks=(
                                        None if existing_step_state else prior_checks
                                    ),
                                    chain_metadata=(None if existing_step_state else step.chain_metadata),
                                )
                            state = _read_state(step_workdir)
                            completed_step = state.get("last_verdict") == "completed"
                            step_record: dict[str, Any] = {
                                "edit_index": step_index,
                                "edit_id": step.id,
                                "instruction": step.prompt,
                                "chain_metadata": step.chain_metadata,
                                "workdir": str(step_workdir.resolve()),
                                "status": "ok" if completed_step else "incomplete",
                                "cost_usd": sum((state.get("costs") or {}).values()),
                                "last_completed_phase": state.get("last_completed_phase"),
                                "last_verdict": state.get("last_verdict"),
                            }
                            if not completed_step:
                                step_records.append(step_record)
                                record["steps"] = step_records
                                return
                            step_record["ground_truth"] = _export_edit_ground_truth(
                                step_workdir, edit_id=step.id
                            )
                            checks = _accepted_checks(
                                step_workdir,
                                namespace=f"{task.id}__{step_index:02d}_{step.id}",
                            )
                            prior_checks.append(checks)
                            step_records.append(step_record)
                            source_frontend = step_workdir / "frontend"
                            source_evaluation = _latest_grade_path(step_workdir)
                        record["steps"] = step_records
                        return
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
                        atomic_plan=task.atomic_plan,
                        hidden_oracle_checks=list(task.hidden_oracle_checks),
                    )

                await asyncio.wait_for(execute_case(), timeout=timeout_seconds)
                state = _read_state(workdir)
                if task.edits:
                    steps = record.get("steps") or []
                    completed_run = len(steps) == len(task.edits) and all(
                        item.get("status") == "ok" for item in steps
                    )
                    total_cost = sum(float(item.get("cost_usd") or 0.0) for item in steps)
                    last_phase = steps[-1].get("last_completed_phase") if steps else None
                    last_verdict = steps[-1].get("last_verdict") if steps else None
                else:
                    completed_run = state.get("last_verdict") == "completed"
                    total_cost = sum((state.get("costs") or {}).values())
                    last_phase = state.get("last_completed_phase")
                    last_verdict = state.get("last_verdict")
                    if completed_run and task.task_mode == "edit":
                        record["ground_truth"] = _export_edit_ground_truth(
                            workdir, edit_id=task.id
                        )
                record.update({
                    "status": "ok" if completed_run else "incomplete",
                    "cost_usd": total_cost,
                    "last_completed_phase": last_phase,
                    "last_verdict": last_verdict,
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
    parser.add_argument(
        "--evaluator-evidence-route",
        choices=("llm", "auto", "typed"),
        help=(
            "llm keeps formal full evaluation; auto/typed may skip the LLM only for "
            "complete typed behavior evidence with visual_evidence=not_required"
        ),
    )
    parser.add_argument("--resume-harness", action="store_true")
    args = parser.parse_args()
    overrides = {
        key: value for key, value in {
            "max_rounds": args.max_rounds,
            "max_budget_usd": args.max_budget,
            "planner_model": args.planner_model,
            "generator_model": args.generator_model,
            "evaluator_model": args.evaluator_model,
            "evaluator_evidence_route": args.evaluator_evidence_route,
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
