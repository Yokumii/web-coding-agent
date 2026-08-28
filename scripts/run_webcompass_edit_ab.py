#!/usr/bin/env python3
"""One-case real WebCompass-aligned comparison: full-context bare model vs Harness."""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import platform
import shutil
import sys
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src.agents.openai_runner import OpenAIHTTPClient
from src.config import HarnessConfig
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.harness import run_harness
from src.utils.llm_json import extract_json_object


DEFAULT_INSTANCE_ID = "gen14-web2code-w2c-g2-00051-78bb3925d3"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return


def parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--dataset",
        type=Path,
        default=repo.parent
        / "WebCoding_Data/output/0805_supplement_release_cache/text-edit.jsonl.gz",
    )
    value.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    value.add_argument("--model", default=os.getenv("GENERATOR_MODEL", "qwen3.6-plus"))
    value.add_argument(
        "--output-root",
        type=Path,
        default=repo / "logs/webcompass_edit_ab",
    )
    value.add_argument("--skip-cache-precheck", action="store_true")
    value.add_argument(
        "--reuse-bare-run",
        type=Path,
        help="Reuse a prior run directory's completed bare arm without another paid request.",
    )
    value.add_argument(
        "--reuse-harness-plan-run",
        type=Path,
        help="Reuse a prior run's accepted planner checkpoint on a fresh source baseline.",
    )
    value.add_argument(
        "--reuse-harness-run",
        type=Path,
        help="Reuse a prior completed Harness arm and rerun only the shared browser contract.",
    )
    return value


def load_case(dataset: Path, instance_id: str) -> dict[str, Any]:
    with gzip.open(dataset, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("instance_id") == instance_id:
                return row
    raise ValueError(f"instance not found: {instance_id}")


def task_description(row: dict[str, Any]) -> str:
    task_types = set(row.get("task_type") or [])
    matches = [
        str(item.get("description") or "").strip()
        for item in (row.get("instruction") or {}).get("description") or []
        if not task_types or item.get("task_type") in task_types
    ]
    output = "\n".join(item for item in matches if item)
    if not output:
        raise ValueError("selected Edit record has no task description")
    return output


def source_files(row: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"path": str(item["path"]), "code": str(item["code"])}
        for item in (row.get("instruction") or {}).get("src_code") or []
    ]


def reference_patches(row: dict[str, Any]) -> list[dict[str, str]]:
    task_types = set(row.get("task_type") or [])
    return [
        {
            "path": str(item["path"]),
            "search": str(item["search"]),
            "replace": str(item["replace"]),
        }
        for item in row.get("response") or []
        if not task_types or item.get("task_type") in task_types
    ]


def materialize_source(row: dict[str, Any], workdir: Path) -> None:
    frontend = workdir / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    for item in source_files(row):
        target = frontend / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["code"], encoding="utf-8")


def apply_exact_patches(frontend: Path, patches: list[dict[str, str]]) -> None:
    for index, patch in enumerate(patches):
        target = (frontend / patch["path"]).resolve()
        try:
            target.relative_to(frontend.resolve())
        except ValueError as exc:
            raise ValueError(f"patch {index} escapes frontend: {patch['path']}") from exc
        if not target.is_file():
            raise ValueError(f"patch {index} targets missing source: {patch['path']}")
        content = target.read_text(encoding="utf-8")
        if not patch["search"] or content.count(patch["search"]) != 1:
            raise ValueError(f"patch {index} search is not one exact occurrence")
        target.write_text(
            content.replace(patch["search"], patch["replace"], 1),
            encoding="utf-8",
        )


def file_hashes(frontend: Path) -> dict[str, str]:
    return {
        path.relative_to(frontend).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(frontend.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def shared_browser_checks() -> list[dict[str, Any]]:
    control = (
        "button:has-text('Hide'), button:has-text('Show'), "
        "button[aria-label*='Transit' i], input[type='checkbox'][id*='transit' i], "
        "input[type='checkbox'][aria-label*='Transit' i], "
        "button[data-testid*='transit' i], input[data-testid*='transit' i]"
    )
    target = (
        "#transit-info, [data-testid='public-transit-block'], "
        ".info-block:has(h2:has-text('Public Transit'))"
    )
    return [
        {
            "id": "TRANSIT-INITIAL-HIDDEN",
            "route": "/",
            "actions": [
                {"action": "assert_visible", "selector": control},
                {"action": "assert_hidden", "selector": target},
                {"action": "click", "selector": control},
                {"action": "assert_visible", "selector": target},
                {
                    "action": "assert_text",
                    "selector": target,
                    "value": "Public Transit",
                    "match": "contains",
                },
                {"action": "click", "selector": control},
                {"action": "assert_hidden", "selector": target},
            ],
        },
        {
            "id": "TRANSIT-INITIAL-VISIBLE",
            "route": "/",
            "actions": [
                {"action": "assert_visible", "selector": control},
                {"action": "assert_visible", "selector": target},
                {"action": "click", "selector": control},
                {"action": "assert_hidden", "selector": target},
                {"action": "assert_visible", "selector": control},
                {"action": "click", "selector": control},
                {"action": "assert_visible", "selector": target},
                {
                    "action": "assert_text",
                    "selector": target,
                    "value": "Public Transit",
                    "match": "contains",
                },
            ],
        }
    ]


async def observe(frontend: Path, output: Path) -> dict[str, Any]:
    handler = partial(QuietHandler, directory=str(frontend))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{server.server_address[1]}",
            checks=shared_browser_checks(),
            output_path=output,
            headless=True,
            action_timeout_ms=1500,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    return int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)


async def cache_precheck(
    config: HarnessConfig, model: str, output: Path
) -> dict[str, Any]:
    client = OpenAIHTTPClient(config, 60)
    stable_prefix = "Web edit cache precheck. " + ("preserve accepted source exactly; " * 450)
    records: list[dict[str, Any]] = []
    for request_index in (1, 2):
        response = await client.complete(
            model=model,
            messages=[
                {"role": "system", "content": stable_prefix},
                {"role": "user", "content": "Reply with OK."},
            ],
            temperature=0,
            max_tokens=2,
        )
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        record = {
            "status": "ok",
            "request_index": request_index,
            "usage": usage,
            "cached_tokens": _cached_tokens(usage),
        }
        records.append(record)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    return {
        "status": "hit" if records[-1]["cached_tokens"] > 0 else "no_hit",
        "second_cached_tokens": records[-1]["cached_tokens"],
    }


def bare_prompt(row: dict[str, Any]) -> str:
    blocks = [
        f"<file path={json.dumps(item['path'])}>\n{item['code']}\n</file>"
        for item in source_files(row)
    ]
    return (
        "Apply this one frontend Edit to the provided complete project source:\n\n"
        + task_description(row)
        + "\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn JSON only as {\"patches\":[{\"path\":...,\"search\":...,\"replace\":...}]}. "
        "Every search string must identify exactly one occurrence in the current source. "
        "Preserve unrelated source and do not return full files."
    )


def normalize_model_patches(payload: dict[str, Any]) -> list[dict[str, str]]:
    output = []
    for item in payload.get("patches") or []:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "path": str(item.get("path") or ""),
                "search": str(item.get("search") or ""),
                "replace": str(item.get("replace") or ""),
            }
        )
    if not output:
        raise ValueError("bare model returned no patches")
    return output


def append_result(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def _changed_paths(source: Path, candidate: Path) -> list[str]:
    before = file_hashes(source)
    after = file_hashes(candidate)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _browser_passed(evidence: dict[str, Any]) -> bool:
    checks = evidence.get("checks") or []
    # The user asked for show/hide behavior, not one prescribed DOM shape.
    # Both arms receive the same two accepted implementation contracts.
    return any(item.get("status") == "ok" for item in checks)


def _phase_token_totals(state: dict[str, Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0}
    for key, metric in (state.get("phase_metrics") or {}).items():
        if key != "planner" and not key.startswith("generator_r"):
            continue
        usage = metric.get("token_usage") or {}
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
    return totals


def actual_harness_source_exposure(workdir: Path) -> dict[str, Any]:
    frontend = workdir / "frontend"
    harness = workdir / ".harness"
    lines_by_path: dict[str, list[str]] = {}
    exposed: dict[str, set[int]] = {}
    for path in sorted(frontend.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            relative = path.relative_to(workdir).as_posix()
            lines_by_path[relative] = path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines(keepends=True)
    for context_path in harness.glob("edit_context_round_*.json"):
        context = json.loads(context_path.read_text(encoding="utf-8"))
        for window in context.get("source_windows") or []:
            relative = str(window.get("path") or "")
            exposed.setdefault(relative, set()).update(
                range(int(window.get("start_line") or 1), int(window.get("end_line") or 0) + 1)
            )
        for outline in context.get("source_outlines") or []:
            rendered = context.get("rendered_outline_paths")
            if isinstance(rendered, list) and str(outline.get("path") or "") not in rendered:
                continue
            relative = str(outline.get("path") or "")
            exposed.setdefault(relative, set()).update(
                int(item["line"])
                for item in outline.get("entries") or []
                if isinstance(item, dict) and item.get("line")
            )
    for trace_path in harness.glob("traces/generator_round_*.jsonl"):
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") != "assistant":
                continue
            for call in (event.get("message") or {}).get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict) or function.get("name") != "read_file":
                    continue
                raw = function.get("arguments") or "{}"
                args = json.loads(raw) if isinstance(raw, str) else raw
                relative = str((args or {}).get("path") or "")
                if relative not in lines_by_path:
                    continue
                start = max(1, int((args or {}).get("start_line") or 1))
                end = min(
                    len(lines_by_path[relative]),
                    int((args or {}).get("end_line") or len(lines_by_path[relative])),
                )
                exposed.setdefault(relative, set()).update(range(start, end + 1))
    full_chars = sum(len(line) for lines in lines_by_path.values() for line in lines)
    exposed_chars = sum(
        len(lines_by_path[path][line - 1])
        for path, line_numbers in exposed.items()
        if path in lines_by_path
        for line in line_numbers
        if 1 <= line <= len(lines_by_path[path])
    )
    return {
        "full_source_chars": full_chars,
        "exposed_source_chars": exposed_chars,
        "ratio": round(exposed_chars / full_chars, 6) if full_chars else 0.0,
        "paths": sorted(path for path, items in exposed.items() if items),
    }


def reuse_harness_plan(prior_run: Path, workdir: Path) -> None:
    prior_harness = prior_run.resolve() / "harness" / ".harness"
    if not (prior_harness / "harness_state.json").is_file():
        raise ValueError(f"prior run has no planner checkpoint: {prior_run}")
    prepare_edit_task_contract(workdir, requested_target_routes=["/"])
    current_harness = workdir / ".harness"
    artifacts = (
        "accepted_sprints.json",
        "atomic_edit_plan.json",
        "design_tokens.json",
        "edit_card.json",
        "feature_list.json",
        "harness_state.json",
        "progress.md",
        "spec.md",
        "sprint_plan.json",
        "target_profile.json",
        "task_inputs.json",
        "ui_verification_plan.json",
    )
    for name in artifacts:
        source = prior_harness / name
        if source.is_file():
            shutil.copy2(source, current_harness / name)
    prior_trace = prior_harness / "traces" / "planner.jsonl"
    if prior_trace.is_file():
        (current_harness / "traces").mkdir(parents=True, exist_ok=True)
        shutil.copy2(prior_trace, current_harness / "traces" / "planner.jsonl")


async def run_bare_arm(
    *, row: dict[str, Any], run_dir: Path, config: HarnessConfig, model: str
) -> dict[str, Any]:
    workdir = run_dir / "bare"
    materialize_source(row, workdir)
    started = time.monotonic()
    client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
    response = await client.complete(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a frontend code editor. Return only the requested exact patch JSON.",
            },
            {"role": "user", "content": bare_prompt(row)},
        ],
        temperature=0,
        max_tokens=4096,
    )
    raw = str((response.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    (run_dir / "bare_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    patches = normalize_model_patches(extract_json_object(raw))
    apply_exact_patches(workdir / "frontend", patches)
    evidence = await observe(workdir / "frontend", run_dir / "bare_browser_evidence.json")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "status": "ok",
        "arm": "bare_full_context",
        "duration_seconds": round(time.monotonic() - started, 3),
        "usage": usage,
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "full_source_chars_in_prompt": sum(len(item["code"]) for item in source_files(row)),
        "patch_count": len(patches),
        "browser_passed": _browser_passed(evidence),
    }


async def reuse_bare_arm(
    *, row: dict[str, Any], run_dir: Path, prior_run: Path
) -> dict[str, Any]:
    prior_run = prior_run.resolve()
    source_frontend = prior_run / "bare" / "frontend"
    response_path = prior_run / "bare_response.json"
    if not source_frontend.is_dir() or not response_path.is_file():
        raise ValueError(f"prior run has no completed bare arm: {prior_run}")
    shutil.copytree(source_frontend, run_dir / "bare" / "frontend")
    response = json.loads(response_path.read_text(encoding="utf-8"))
    (run_dir / "bare_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    raw = str((response.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    patches = normalize_model_patches(extract_json_object(raw))
    evidence = await observe(run_dir / "bare" / "frontend", run_dir / "bare_browser_evidence.json")
    return {
        "status": "ok",
        "arm": "bare_full_context",
        "duration_seconds": 0.0,
        "reused_from": str(prior_run),
        "usage": usage,
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "full_source_chars_in_prompt": sum(len(item["code"]) for item in source_files(row)),
        "patch_count": len(patches),
        "browser_passed": _browser_passed(evidence),
    }


async def run_harness_arm(
    *,
    row: dict[str, Any],
    run_dir: Path,
    config: HarnessConfig,
    prior_plan_run: Path | None = None,
) -> dict[str, Any]:
    workdir = run_dir / "harness"
    materialize_source(row, workdir)
    if prior_plan_run is not None:
        reuse_harness_plan(prior_plan_run, workdir)
    started = time.monotonic()
    await run_harness(
        task_description(row),
        workdir,
        config,
        resume=prior_plan_run is not None,
        task_mode="edit",
        target_routes=["/"],
    )
    evidence = await observe(workdir / "frontend", run_dir / "harness_browser_evidence.json")
    state = FileComm(workdir / ".harness").read_state() or {}
    context_files = sorted((workdir / ".harness").glob("edit_context_round_*.json"))
    contexts = [json.loads(path.read_text(encoding="utf-8")) for path in context_files]
    return {
        "status": "ok",
        "arm": "harness_local_context",
        "duration_seconds": round(time.monotonic() - started, 3),
        "usage": _phase_token_totals(state),
        "input_tokens": _phase_token_totals(state)["input_tokens"],
        "output_tokens": _phase_token_totals(state)["output_tokens"],
        "source_exposure": [item.get("exposure") or {} for item in contexts],
        "actual_source_exposure": actual_harness_source_exposure(workdir),
        "browser_passed": _browser_passed(evidence),
        "last_completed_phase": state.get("last_completed_phase"),
        "last_verdict": state.get("last_verdict"),
        "rounds_with_context": len(contexts),
    }


async def reuse_harness_arm(
    *, run_dir: Path, prior_run: Path
) -> dict[str, Any]:
    prior_run = prior_run.resolve()
    source_workdir = prior_run / "harness"
    if not (source_workdir / "frontend").is_dir():
        raise ValueError(f"prior run has no completed Harness arm: {prior_run}")
    workdir = run_dir / "harness"
    shutil.copytree(source_workdir, workdir)
    evidence = await observe(
        workdir / "frontend", run_dir / "harness_browser_evidence.json"
    )
    state = FileComm(workdir / ".harness").read_state() or {}
    contexts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((workdir / ".harness").glob("edit_context_round_*.json"))
    ]
    totals = _phase_token_totals(state)
    return {
        "status": "ok",
        "arm": "harness_local_context",
        "duration_seconds": 0.0,
        "reused_from": str(prior_run),
        "usage": totals,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "source_exposure": [item.get("exposure") or {} for item in contexts],
        "actual_source_exposure": actual_harness_source_exposure(workdir),
        "browser_passed": _browser_passed(evidence),
        "last_completed_phase": state.get("last_completed_phase"),
        "last_verdict": state.get("last_verdict"),
        "rounds_with_context": len(contexts),
    }


def make_harness_arm_runner(
    *,
    row: dict[str, Any],
    run_dir: Path,
    config: HarnessConfig,
    prior_plan_run: Path | None,
    prior_completed_run: Path | None,
):
    if prior_completed_run is not None:
        return lambda: reuse_harness_arm(
            run_dir=run_dir, prior_run=prior_completed_run
        )
    return lambda: run_harness_arm(
        row=row,
        run_dir=run_dir,
        config=config,
        prior_plan_run=prior_plan_run,
    )


def finalize_arm(
    payload: dict[str, Any], *, source: Path, candidate: Path, reference: Path
) -> dict[str, Any]:
    changed = _changed_paths(source, candidate)
    reference_changed = _changed_paths(source, reference)
    return {
        **payload,
        "changed_paths": changed,
        "reference_changed_paths": reference_changed,
        "unrelated_changed_paths": sorted(set(changed) - set(reference_changed)),
        "exact_reference_match": file_hashes(candidate) == file_hashes(reference),
        "success": bool(payload.get("browser_passed"))
        and not (set(changed) - set(reference_changed)),
    }


async def main() -> int:
    args = parser().parse_args()
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + f"_{args.instance_id}"
    run_dir = args.output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    results_path = run_dir / "results.jsonl"
    row = load_case(args.dataset.resolve(), args.instance_id)
    (run_dir / "case.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metadata = {
        "status": "running",
        "command": [sys.executable, *sys.argv],
        "python": platform.python_version(),
        "model": args.model,
        "dataset": str(args.dataset.resolve()),
        "instance_id": args.instance_id,
        "task_type": row.get("task_type"),
        "comparison_order": ["bare_full_context", "harness_local_context"],
        "evaluator_mode": "simple_plus_shared_browser_contract",
        "automatic_paid_retries": 0,
        "reused_bare_run": str(args.reuse_bare_run.resolve()) if args.reuse_bare_run else None,
        "reused_harness_plan_run": (
            str(args.reuse_harness_plan_run.resolve())
            if args.reuse_harness_plan_run
            else None
        ),
        "reused_harness_run": (
            str(args.reuse_harness_run.resolve()) if args.reuse_harness_run else None
        ),
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    config = HarnessConfig(
        agent_runtime="openai",
        planner_model=args.model,
        generator_model=args.model,
        evaluator_model=args.model,
        evaluator_mode="simple",
        playwright_headless=True,
        edit_max_rounds=2,
        planner_max_turns=4,
        generator_max_turns=10,
        evaluator_max_turns=10,
        agent_max_tool_calls=30,
        max_budget_usd=10.0,
        planner_budget_usd=1.0,
        generator_budget_usd=5.0,
        evaluator_budget_usd=2.0,
    )
    if not args.skip_cache_precheck:
        metadata["cache_precheck"] = await cache_precheck(
            config, args.model, run_dir / "cache_precheck.jsonl"
        )
        (run_dir / "run_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    source_workdir = run_dir / "source"
    reference_workdir = run_dir / "reference"
    materialize_source(row, source_workdir)
    shutil.copytree(source_workdir / "frontend", reference_workdir / "frontend")
    apply_exact_patches(reference_workdir / "frontend", reference_patches(row))

    arm_specs = [
        (
            "bare_full_context",
            (
                (lambda: reuse_bare_arm(row=row, run_dir=run_dir, prior_run=args.reuse_bare_run))
                if args.reuse_bare_run
                else (lambda: run_bare_arm(row=row, run_dir=run_dir, config=config, model=args.model))
            ),
            run_dir / "bare" / "frontend",
        ),
        (
            "harness_local_context",
            make_harness_arm_runner(
                row=row,
                run_dir=run_dir,
                config=config,
                prior_plan_run=args.reuse_harness_plan_run,
                prior_completed_run=args.reuse_harness_run,
            ),
            run_dir / "harness" / "frontend",
        ),
    ]
    for arm, runner, candidate in arm_specs:
        try:
            payload = await runner()
            payload = finalize_arm(
                payload,
                source=source_workdir / "frontend",
                candidate=candidate,
                reference=reference_workdir / "frontend",
            )
        except Exception as exc:
            payload = {"status": "error", "arm": arm, "error": f"{type(exc).__name__}: {exc}"}
        append_result(results_path, payload)
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    records = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
    summary = {
        "status": "ok" if all(item.get("status") == "ok" for item in records) else "partial",
        "instance_id": args.instance_id,
        "results": records,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metadata["status"] = summary["status"]
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
