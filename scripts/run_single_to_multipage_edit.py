#!/usr/bin/env python3
"""Run one real fake-multi-page seed through the Edit Harness into physical pages."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.export_trajectory_dataset import (
    append_jsonl_records,
    export_run,
    to_v2_records,
)
from scripts.prepare_forward_edit_seed import prepare_seed
from scripts.run_webcompass_edit_ab import (
    _phase_token_totals,
    actual_harness_source_exposure,
    append_result,
    cache_precheck,
    file_hashes,
    load_case,
    materialize_source,
)
from src.config import HarnessConfig
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.harness import run_harness
from src.orchestration.hidden_oracle_checks import write_hidden_oracle_checks


DEFAULT_INSTANCE_ID = "gen14-webgen-bench-wg-2-00068-930c5d58bf"
EDIT_PROMPT = (
    "Turn the current hash-routed Energy Initiative Tracker into a real two-page "
    "static site: keep Dashboard at `/` (`index.html`) and move Settings to the "
    "direct-loadable `/settings.html` page. Replace the hash navigation with "
    "same-origin physical links whose href values are exactly `/` and "
    "`/settings.html`; keep the shared `styles.css` and `app.js`, the existing "
    "visual design and content, dashboard category filtering, settings form save "
    "and success toast, and persist both the selected dashboard category and saved "
    "preferences across physical page navigation. Do not add unrelated features, "
    "dependencies, or external assets."
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        # Chromium may request an undeclared favicon even when the page has no
        # favicon link. Keep that browser-generated request from obscuring real
        # JS/CSS/page resource failures in assert_no_console_errors.
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()


def parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--dataset",
        type=Path,
        default=repo.parent
        / "output/0805_supplement_release_cache/text-edit.jsonl.gz",
    )
    value.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    value.add_argument("--model", default=os.getenv("GENERATOR_MODEL", "qwen3.6-plus"))
    value.add_argument(
        "--output-root",
        type=Path,
        default=repo / "logs/single_to_multipage_edit",
    )
    value.add_argument(
        "--resume-run",
        type=Path,
        help=(
            "Resume one preserved run from its trace-proven Planner checkpoint; "
            "does not repeat cache precheck or Planner requests."
        ),
    )
    value.add_argument(
        "--reuse-planner-run",
        type=Path,
        help=(
            "Create a fresh source run but reuse one completed trace-proven Planner "
            "checkpoint; no cache or Planner requests are repeated."
        ),
    )
    value.add_argument(
        "--revalidate-run",
        type=Path,
        help=(
            "Copy one preserved committed run and repeat deterministic Harness/browser "
            "evaluation only. No Planner or Generator request is made."
        ),
    )
    value.add_argument("--skip-cache-precheck", action="store_true")
    return value


def source_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": "SOURCE-DASHBOARD-FILTER",
            "route": "/#/dashboard",
            "actions": [
                {"action": "assert_visible", "selector": ".dashboard-grid"},
                {"action": "click", "selector": ".category-card[data-category='solar']"},
                {
                    "action": "assert_text",
                    "selector": ".records-header span",
                    "value": "Solar Energy",
                    "match": "contains",
                },
                {"action": "assert_count", "selector": "tbody tr", "count": 2},
                {"action": "assert_no_console_errors"},
            ],
        },
        {
            "id": "SOURCE-SETTINGS-SAVE",
            "route": "/#/settings",
            "actions": [
                {"action": "assert_visible", "selector": "#prefs-form"},
                {"action": "fill", "selector": "#displayName", "value": "Harness E2E"},
                {"action": "select_option", "selector": "#density", "value": "compact"},
                {"action": "click", "selector": "#prefs-form button[type='submit']"},
                {
                    "action": "assert_text",
                    "selector": ".toast",
                    "value": "Preferences saved successfully",
                    "match": "contains",
                },
                {
                    "action": "assert_storage_value",
                    "storage": "local",
                    "key": "eit_prefs",
                    "value": "Harness E2E",
                    "match": "contains",
                },
                {"action": "assert_no_console_errors"},
            ],
        },
    ]


def target_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": "TARGET-PHYSICAL-NAVIGATION-AND-STATE",
            "route": "/",
            "actions": [
                {"action": "assert_url", "value": "/"},
                {"action": "assert_hash", "value": ""},
                {"action": "assert_visible", "selector": ".dashboard-grid"},
                {"action": "click", "selector": ".category-card[data-category='solar']"},
                {
                    "action": "assert_text",
                    "selector": ".records-header span",
                    "value": "Solar Energy",
                    "match": "contains",
                },
                {"action": "assert_count", "selector": "tbody tr", "count": 2},
                {
                    "action": "click",
                    "selector": "nav a[href='/settings.html']",
                    "settle_ms": 200,
                },
                {"action": "assert_url", "value": "/settings.html"},
                {"action": "assert_hash", "value": ""},
                {"action": "assert_visible", "selector": "#prefs-form"},
                {"action": "fill", "selector": "#displayName", "value": "Harness E2E"},
                {"action": "select_option", "selector": "#density", "value": "compact"},
                {"action": "click", "selector": "#prefs-form button[type='submit']"},
                {
                    "action": "assert_text",
                    "selector": ".toast",
                    "value": "Preferences saved successfully",
                    "match": "contains",
                },
                {
                    "action": "assert_storage_value",
                    "storage": "local",
                    "key": "eit_prefs",
                    "value": "Harness E2E",
                    "match": "contains",
                },
                {
                    "action": "click",
                    "selector": "nav a[href='/']",
                    "settle_ms": 200,
                },
                {"action": "assert_url", "value": "/"},
                {
                    "action": "assert_visible",
                    "selector": ".category-card.selected[data-category='solar']",
                },
                {"action": "assert_no_console_errors"},
            ],
        },
        {
            "id": "TARGET-SETTINGS-DIRECT-LOAD",
            "route": "/settings.html",
            "actions": [
                {"action": "assert_url", "value": "/settings.html"},
                {"action": "assert_hash", "value": ""},
                {"action": "assert_visible", "selector": "#prefs-form"},
                {"action": "assert_value", "selector": "#displayName", "value": "Harness E2E"},
                {"action": "assert_value", "selector": "#density", "value": "compact"},
                {"action": "assert_no_console_errors"},
            ],
        },
    ]


async def observe(
    frontend: Path, checks: list[dict[str, Any]], output: Path
) -> dict[str, Any]:
    handler = partial(QuietHandler, directory=str(frontend))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{server.server_address[1]}",
            checks=checks,
            output_path=output,
            headless=True,
            fail_fast=False,
            action_timeout_ms=2_000,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def browser_passed(evidence: dict[str, Any]) -> bool:
    checks = evidence.get("checks") or []
    return bool(checks) and all(item.get("status") == "ok" for item in checks)


def reserve_frontend_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def changed_paths(source: Path, target: Path) -> list[str]:
    before = file_hashes(source)
    after = file_hashes(target)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def physical_html_files(frontend: Path) -> list[str]:
    return sorted(
        path.relative_to(frontend).as_posix()
        for path in frontend.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".html", ".htm"}
        and ".git" not in path.parts
    )


def export_accepted_dataset(workdir: Path, output_dir: Path) -> dict[str, Any]:
    records = export_run(workdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    appended = append_jsonl_records(output_dir / "records.jsonl", records)
    v2_counts: dict[str, int] = {}
    for name, rows in to_v2_records(records).items():
        append_jsonl_records(output_dir / "v2" / f"{name}.jsonl", rows)
        v2_counts[name] = len(rows)
    return {
        "records": len(records),
        "appended": appended,
        "task_counts": {
            task: sum(item.get("task") == task for item in records)
            for task in ("text-generation", "text-editing", "text-repair")
        },
        "v2_counts": v2_counts,
    }


def harness_config(model: str) -> HarnessConfig:
    return HarnessConfig(
        agent_runtime="openai",
        planner_model=model,
        generator_model=model,
        evaluator_model=model,
        evaluator_mode="simple",
        playwright_headless=True,
        frontend_port=reserve_frontend_port(),
        edit_max_rounds=1,
        planner_max_turns=4,
        generator_max_turns=12,
        evaluator_max_turns=1,
        agent_max_tool_calls=28,
        max_budget_usd=8.0,
        planner_budget_usd=1.0,
        generator_budget_usd=6.0,
        evaluator_budget_usd=1.0,
    )


def install_hidden_oracle(workdir: Path) -> None:
    write_hidden_oracle_checks(
        workdir / ".harness",
        target_checks(),
        target_routes=["/", "/settings.html"],
    )


def reuse_planner_checkpoint(prior_run: Path, workdir: Path) -> None:
    prior_harness = prior_run.resolve() / "harness" / ".harness"
    prior_state = FileComm(prior_harness).read_state() or {}
    if prior_state.get("last_completed_phase") != "plan":
        raise ValueError(
            f"prior run has no reusable plan-only checkpoint: {prior_run}"
        )
    prepare_edit_task_contract(
        workdir, requested_target_routes=["/", "/settings.html"]
    )
    install_hidden_oracle(workdir)
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
        if not source.is_file():
            raise ValueError(f"prior Planner checkpoint is missing {name}: {prior_run}")
        shutil.copy2(source, current_harness / name)
    prior_trace = prior_harness / "traces" / "planner.jsonl"
    if not prior_trace.is_file():
        raise ValueError(f"prior Planner checkpoint has no trace: {prior_run}")
    (current_harness / "traces").mkdir(parents=True, exist_ok=True)
    shutil.copy2(prior_trace, current_harness / "traces" / "planner.jsonl")


async def resume_run(args: argparse.Namespace) -> int:
    run_dir = args.resume_run.resolve()
    workdir = run_dir / "harness"
    source_frontend = run_dir / "source" / "frontend"
    if not workdir.is_dir() or not source_frontend.is_dir():
        raise ValueError(f"resume run is missing source/harness directories: {run_dir}")
    results_path = run_dir / "results.jsonl"
    resume_stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    resume_metadata = {
        "status": "running",
        "resume_stamp": resume_stamp,
        "run_dir": str(run_dir),
        "model": args.model,
        "resume_policy": "trace_proven_planner_no_repeat",
        "automatic_paid_retries": 0,
        "edit_round_limit": 1,
    }
    metadata_path = run_dir / f"resume_metadata_{resume_stamp}.json"
    metadata_path.write_text(
        json.dumps(resume_metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    exit_code = 1
    try:
        await run_harness(
            EDIT_PROMPT,
            workdir,
            harness_config(args.model),
            resume=True,
            task_mode="edit",
            target_routes=["/", "/settings.html"],
        )
        state = FileComm(workdir / ".harness").read_state() or {}
        target_evidence = await observe(
            workdir / "frontend",
            target_checks(),
            run_dir / f"target_browser_evidence_{resume_stamp}.json",
        )
        target_html = physical_html_files(workdir / "frontend")
        all_checks_passed = browser_passed(target_evidence)
        accepted = state.get("last_verdict") == "completed"
        success = accepted and all_checks_passed and len(target_html) >= 2
        dataset_export = (
            export_accepted_dataset(workdir, run_dir / "dataset")
            if accepted
            else None
        )
        append_result(
            results_path,
            {
                "status": "ok" if success else "error",
                "stage": "resumed_target_validation",
                "resume_stamp": resume_stamp,
                "success": success,
                "harness_accepted": accepted,
                "last_completed_phase": state.get("last_completed_phase"),
                "last_verdict": state.get("last_verdict"),
                "browser_passed": all_checks_passed,
                "physical_html_files": target_html,
                "physical_html_count": len(target_html),
                "changed_paths": changed_paths(
                    source_frontend, workdir / "frontend"
                ),
                "duration_seconds": round(time.monotonic() - started, 3),
                "usage": _phase_token_totals(state),
                "source_exposure": actual_harness_source_exposure(workdir),
                "dataset_export": dataset_export,
            },
        )
        exit_code = 0 if success else 1
    except Exception as exc:
        append_result(
            results_path,
            {
                "status": "error",
                "stage": "resume_run",
                "resume_stamp": resume_stamp,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    records = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = {
        "status": "ok" if exit_code == 0 else "error",
        "resume_stamp": resume_stamp,
        "run_dir": str(run_dir),
        "records": records,
    }
    (run_dir / f"resume_summary_{resume_stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    resume_metadata["status"] = summary["status"]
    metadata_path.write_text(
        json.dumps(resume_metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return exit_code


async def revalidate_run(args: argparse.Namespace) -> int:
    prior_run = args.revalidate_run.resolve()
    prior_workdir = prior_run / "harness"
    prior_source = prior_run / "source"
    if not prior_workdir.is_dir() or not prior_source.is_dir():
        raise ValueError(f"revalidation source is incomplete: {prior_run}")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = args.output_root.resolve() / (
        f"{stamp}_revalidation_{args.instance_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(prior_source, run_dir / "source")
    shutil.copytree(prior_workdir, run_dir / "harness")
    workdir = run_dir / "harness"
    file_comm = FileComm(workdir / ".harness")
    install_hidden_oracle(workdir)
    # This is a fresh copied revalidation workdir. Remove only copied
    # evaluator outputs whose verdict depends on the evaluator implementation;
    # immutable source, model traces, commits, and the original run stay intact.
    for name in (
        "browser_evidence_round_1.json",
        "feedback_round_1.md",
        "grade_round_1.json",
        "hidden_oracle_evidence_round_1.json",
        "minimality_round_1_edit.json",
        "repair_packet_round_1.json",
    ):
        (file_comm.dir / name).unlink(missing_ok=True)
    copied_minimality = file_comm.dir / "minimality" / "round_1_edit"
    if copied_minimality.is_dir():
        shutil.rmtree(copied_minimality)
    state = file_comm.read_state() or {}
    if state.get("last_completed_phase") != "evaluate_r1":
        raise ValueError(
            "revalidation requires one committed run that reached evaluate_r1"
        )
    state["last_completed_phase"] = "build_r1"
    state["last_verdict"] = "awaiting_review"
    state["generator_mode"] = "generate"
    state["timestamp"] = datetime.now().isoformat()
    file_comm.write_state(state)
    metadata = {
        "status": "running",
        "run_dir": str(run_dir),
        "source_run": str(prior_run),
        "model_requests": 0,
        "mode": "deterministic_revalidation_only",
    }
    metadata_path = run_dir / "run_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    results_path = run_dir / "results.jsonl"
    started = time.monotonic()
    exit_code = 1
    try:
        await run_harness(
            EDIT_PROMPT,
            workdir,
            harness_config(args.model),
            resume=True,
            task_mode="edit",
            target_routes=["/", "/settings.html"],
        )
        final_state = file_comm.read_state() or {}
        evidence = await observe(
            workdir / "frontend",
            target_checks(),
            run_dir / "target_browser_evidence.json",
        )
        html_files = physical_html_files(workdir / "frontend")
        accepted = final_state.get("last_verdict") == "completed"
        passed = browser_passed(evidence)
        success = accepted and passed and len(html_files) >= 2
        dataset_export = (
            export_accepted_dataset(workdir, run_dir / "dataset")
            if accepted
            else None
        )
        append_result(
            results_path,
            {
                "status": "ok" if success else "error",
                "stage": "deterministic_revalidation",
                "success": success,
                "harness_accepted": accepted,
                "browser_passed": passed,
                "physical_html_files": html_files,
                "physical_html_count": len(html_files),
                "changed_paths": changed_paths(
                    run_dir / "source" / "frontend", workdir / "frontend"
                ),
                "duration_seconds": round(time.monotonic() - started, 3),
                "usage": _phase_token_totals(final_state),
                "source_exposure": actual_harness_source_exposure(workdir),
                "dataset_export": dataset_export,
            },
        )
        exit_code = 0 if success else 1
    except Exception as exc:
        append_result(
            results_path,
            {
                "status": "error",
                "stage": "deterministic_revalidation",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    records = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = {
        "status": "ok" if exit_code == 0 else "error",
        "run_dir": str(run_dir),
        "source_run": str(prior_run),
        "records": records,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    metadata["status"] = summary["status"]
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return exit_code


async def main() -> int:
    args = parser().parse_args()
    if args.revalidate_run is not None:
        return await revalidate_run(args)
    if args.resume_run is not None:
        return await resume_run(args)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + f"_{args.instance_id}"
    run_dir = args.output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    results_path = run_dir / "results.jsonl"
    metadata = {
        "status": "running",
        "command": [sys.executable, *sys.argv],
        "python": platform.python_version(),
        "model": args.model,
        "dataset": str(args.dataset.resolve()),
        "instance_id": args.instance_id,
        "task_mode": "edit",
        "target_routes": ["/", "/settings.html"],
        "automatic_paid_retries": 0,
        "edit_round_limit": 1,
        "decisive_evidence": "typed_dom_state_and_direct_path_navigation",
        "reused_planner_run": (
            str(args.reuse_planner_run.resolve())
            if args.reuse_planner_run is not None
            else None
        ),
    }
    metadata_path = run_dir / "run_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    exit_code = 1
    try:
        row = load_case(args.dataset.resolve(), args.instance_id)
        (run_dir / "case.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        source_workdir = run_dir / "source"
        materialize_source(row, source_workdir)
        source_frontend = source_workdir / "frontend"
        source_html = physical_html_files(source_frontend)
        source_evidence = await observe(
            source_frontend, source_checks(), run_dir / "source_browser_evidence.json"
        )
        source_payload = {
            "status": "ok" if browser_passed(source_evidence) else "error",
            "stage": "source_validation",
            "browser_passed": browser_passed(source_evidence),
            "physical_html_files": source_html,
            "physical_html_count": len(source_html),
            "declared_project_pages": (row.get("metadata") or {}).get("project_pages"),
            "fake_multi_page_confirmed": len(source_html) == 1,
        }
        append_result(results_path, source_payload)
        if not source_payload["browser_passed"] or len(source_html) != 1:
            raise RuntimeError("selected source did not satisfy the verified fake-multi-page precondition")
        source_evaluation = run_dir / "source_evaluation.json"
        source_evaluation.write_text(
            json.dumps(source_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        workdir = run_dir / "harness"
        prepare_seed(source_frontend, workdir, source_evaluation)
        install_hidden_oracle(workdir)
        config = harness_config(args.model)
        if args.reuse_planner_run is not None:
            reuse_planner_checkpoint(args.reuse_planner_run, workdir)
        if not args.skip_cache_precheck and args.reuse_planner_run is None:
            metadata["cache_precheck"] = await cache_precheck(
                config, args.model, run_dir / "cache_precheck.jsonl"
            )
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        started = time.monotonic()
        await run_harness(
            EDIT_PROMPT,
            workdir,
            config,
            resume=args.reuse_planner_run is not None,
            task_mode="edit",
            target_routes=["/", "/settings.html"],
        )
        state = FileComm(workdir / ".harness").read_state() or {}
        target_evidence = await observe(
            workdir / "frontend",
            target_checks(),
            run_dir / "target_browser_evidence.json",
        )
        target_html = physical_html_files(workdir / "frontend")
        all_checks_passed = browser_passed(target_evidence)
        accepted = state.get("last_verdict") == "completed"
        success = accepted and all_checks_passed and len(target_html) >= 2
        dataset_export = (
            export_accepted_dataset(workdir, run_dir / "dataset")
            if accepted
            else None
        )
        target_payload = {
            "status": "ok" if success else "error",
            "stage": "target_validation",
            "success": success,
            "harness_accepted": accepted,
            "last_completed_phase": state.get("last_completed_phase"),
            "last_verdict": state.get("last_verdict"),
            "browser_passed": all_checks_passed,
            "physical_html_files": target_html,
            "physical_html_count": len(target_html),
            "changed_paths": changed_paths(source_frontend, workdir / "frontend"),
            "duration_seconds": round(time.monotonic() - started, 3),
            "usage": _phase_token_totals(state),
            "source_exposure": actual_harness_source_exposure(workdir),
            "dataset_export": dataset_export,
        }
        append_result(results_path, target_payload)
        exit_code = 0 if success else 1
    except Exception as exc:
        append_result(
            results_path,
            {
                "status": "error",
                "stage": "run",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    records = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = {
        "status": "ok" if exit_code == 0 else "error",
        "run_dir": str(run_dir),
        "instance_id": args.instance_id,
        "records": records,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metadata["status"] = summary["status"]
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
