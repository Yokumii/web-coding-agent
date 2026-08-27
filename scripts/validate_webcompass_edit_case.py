from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import platform
import subprocess
import sys
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src.agents.evaluator import build_deterministic_failure_grades
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.edit_card import materialize_edit_card
from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    ensure_minimal_path_plan,
)
from src.orchestration.repair_packet import write_repair_packet


DEFAULT_INSTANCE_ID = "gen14-artifactsbench-ab-2-00341-f922414943"
DEFAULT_TASK_TYPE = "Pagination"
DEFAULT_DESCRIPTION = (
    "Implement deterministic pagination for the facilities list in discovery.html, "
    "including previous/next controls and page state persistence."
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return


def parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(
        description=(
            "Replay one real WebCompass-aligned multi-page Edit through the "
            "deterministic minimal-path and browser-evidence gates."
        )
    )
    value.add_argument(
        "--dataset",
        type=Path,
        default=repo.parent
        / "WebCoding_Data/output/0805_supplement_release_cache/text-edit.jsonl.gz",
    )
    value.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID)
    value.add_argument("--task-type", default=DEFAULT_TASK_TYPE)
    value.add_argument(
        "--output-root", type=Path, default=repo / "logs/edit_first_20260828"
    )
    return value


def load_case(path: Path, instance_id: str, task_type: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        row = None
        for line in handle:
            candidate = json.loads(line)
            if candidate.get("instance_id") == instance_id:
                row = candidate
                break
    if row is None:
        raise ValueError(f"instance not found: {instance_id}")
    patches = [
        item
        for item in row.get("response") or []
        if isinstance(item, dict) and item.get("task_type") == task_type
    ]
    if not patches:
        raise ValueError(f"task type not found in {instance_id}: {task_type}")
    return row, patches


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_hashes(frontend: Path) -> dict[str, str]:
    return {
        path.relative_to(frontend).as_posix(): sha256(path)
        for path in sorted(frontend.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def materialize_source(row: dict[str, Any], workdir: Path) -> None:
    frontend = workdir / "frontend"
    frontend.mkdir(parents=True)
    for item in row.get("instruction", {}).get("src_code") or []:
        target = frontend / str(item["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item["code"]), encoding="utf-8")


def browser_checks() -> list[dict[str, Any]]:
    impact = ["route:/discovery.html", "feature:pagination"]
    return [
        {
            "id": "PAGINATION-INITIAL",
            "requirement_id": "REQ-PAGINATION",
            "route": "/discovery.html",
            "category": "interaction",
            "critical": True,
            "impact_tags": impact,
            "actions": [
                {
                    "action": "assert_visible",
                    "selector": "#facilities-list .facility-card:first-of-type",
                },
                {"action": "assert_text", "selector": "#current-page", "value": "1"},
                {
                    "action": "assert_property",
                    "selector": "#prev-page",
                    "name": "disabled",
                    "value": True,
                },
                {
                    "action": "assert_aria",
                    "selector": "#next-page",
                    "attribute": "role",
                    "value": "button",
                },
                {
                    "action": "assert_aria",
                    "selector": "#next-page",
                    "attribute": "accessible_name",
                    "value": "Next",
                },
                {
                    "action": "assert_property",
                    "selector": "#next-page",
                    "name": "disabled",
                    "value": False,
                },
            ],
        },
        {
            "id": "PAGINATION-NEXT-AND-PERSIST",
            "requirement_id": "REQ-PAGINATION",
            "route": "/discovery.html",
            "category": "state",
            "critical": True,
            "impact_tags": impact,
            "actions": [
                {"action": "click", "selector": "#next-page"},
                {"action": "assert_text", "selector": "#current-page", "value": "2"},
                {
                    "action": "assert_storage_value",
                    "storage": "session",
                    "key": "discovery_page",
                    "value": "2",
                    "match": "exact",
                },
                {"action": "reload"},
                {"action": "assert_text", "selector": "#current-page", "value": "2"},
                {
                    "action": "assert_property",
                    "selector": "#next-page",
                    "name": "disabled",
                    "value": True,
                },
            ],
        },
        {
            "id": "PROTECTED-COMPARISON-SENTINEL",
            "requirement_id": "REQ-EXISTING-COMPARISON",
            "route": "/comparison.html",
            "category": "regression",
            "critical": True,
            "impact_tags": ["route:/comparison.html", "sentinel:critical"],
            "actions": [
                {
                    "action": "assert_text",
                    "selector": "main h1",
                    "value": "Facility Comparison",
                },
                {"action": "assert_no_console_errors"},
            ],
        },
        {
            "id": "PROTECTED-DASHBOARD-SENTINEL",
            "requirement_id": "REQ-EXISTING-DASHBOARD",
            "route": "/index.html",
            "category": "regression",
            "critical": True,
            "impact_tags": ["route:/index.html", "sentinel:critical"],
            "actions": [
                {"action": "assert_visible", "selector": "#export-btn"},
                {"action": "assert_no_console_errors"},
            ],
        },
    ]


def source_failure_check() -> list[dict[str, Any]]:
    return [
        {
            "id": "PAGINATION-MISSING-BEFORE-EDIT",
            "route": "/discovery.html",
            "actions": [
                {"action": "assert_visible", "selector": "#pagination-controls"}
            ],
        }
    ]


async def observe_site(frontend: Path, checks: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    handler = partial(QuietHandler, directory=str(frontend))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        return await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=checks,
            output_path=output,
            headless=True,
            action_timeout_ms=750,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def build_harness_artifacts(workdir: Path, instruction: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    harness = workdir / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    contract = prepare_edit_task_contract(
        workdir, requested_target_routes=["/discovery.html"]
    )
    verification = {"sprints": [{"sprint": 1, "checks": checks}]}
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sprint = {
        "total_sprints": 1,
        "sprints": [
            {
                "sprint": 1,
                "feature_ids": ["F-PAGINATION"],
                "impact_tags": ["route:/discovery.html", "feature:pagination"],
                "requirement_changes": [
                    {
                        "requirement_id": "REQ-PAGINATION",
                        "relation": "add",
                        "prior_requirement_ids": [],
                        "description": instruction,
                    }
                ],
                "unresolved_conflicts": [],
                "visual_evidence": "not_required",
                "visual_evidence_reason": (
                    "Pagination behavior and persistence are fully observable through "
                    "DOM, accessibility, property, storage, and route sentinels."
                ),
            }
        ],
    }
    card = materialize_edit_card(
        harness_dir=harness,
        instruction_delta=instruction,
        edit_contract=contract,
        sprint_plan=sprint,
        verification_plan=verification,
    )
    plan = ensure_minimal_path_plan(
        workdir=workdir,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=100,
        max_touched_files=2,
    )
    return {"contract": contract, "card": card, "plan": plan}


def apply_authorized_patches(
    workdir: Path, plan: dict[str, Any], patches: list[dict[str, str]]
) -> dict[str, Any]:
    frontend = workdir / "frontend"
    policy = MinimalPathPolicy.from_plan(workdir, plan)
    decisions: list[dict[str, Any]] = []
    observed: set[str] = set()
    cone = plan.get("source_change_cone") or {}
    path_order = list(
        dict.fromkeys(
            [
                *(cone.get("initial_paths") or []),
                *(cone.get("local_paths") or []),
                *(cone.get("dependency_paths") or []),
            ]
        )
    )
    ranks = {path: index for index, path in enumerate(path_order)}
    ordered_patches = sorted(
        enumerate(patches),
        key=lambda item: (ranks.get(f"frontend/{item[1]['path']}", 10_000), item[0]),
    )
    active_path: str | None = None
    for original_index, patch in ordered_patches:
        relative = f"frontend/{patch['path']}"
        target = workdir / relative
        if active_path is not None and relative != active_path:
            active_frontend_path = Path(active_path).relative_to("frontend")
            if active_frontend_path.suffix.lower() in {".js", ".jsx", ".mjs", ".cjs"}:
                command = ["node", "--check", active_frontend_path.as_posix()]
            else:
                command = ["git", "diff", "--check", "--", active_frontend_path.as_posix()]
            validation = subprocess.run(
                command, cwd=frontend, text=True, capture_output=True
            )
            policy.observe_validation(
                ok=validation.returncode == 0,
                output=validation.stdout + validation.stderr,
                tool=" ".join(command),
            )
            if validation.returncode != 0:
                raise RuntimeError(validation.stdout + validation.stderr)
        if relative not in observed:
            policy.observe_result(
                "read_file",
                {"path": relative},
                ok=True,
                output=target.read_text(encoding="utf-8"),
            )
            observed.add(relative)
        tool_input = {
            "path": relative,
            "old_text": patch["search"],
            "new_text": patch["replace"],
        }
        denial = policy.check("apply_patch", tool_input)
        decisions.append(
            {
                "patch_index": original_index,
                "execution_index": len(decisions),
                "path": relative,
                "allowed": denial is None,
                "denial": denial,
            }
        )
        if denial is not None:
            raise RuntimeError(f"ground-truth patch {original_index} denied: {denial}")
        content = target.read_text(encoding="utf-8")
        if content.count(patch["search"]) != 1:
            raise RuntimeError(
                f"ground-truth patch {original_index} is not uniquely replayable"
            )
        target.write_text(content.replace(patch["search"], patch["replace"]), encoding="utf-8")
        policy.observe_result("apply_patch", tool_input, ok=True, output="applied")
        active_path = relative

    comparison_probe = {
        "path": "frontend/app.js",
        "old_text": "const Comparison = {",
        "new_text": "const Comparison = { changed: true,",
    }
    comparison_denial = policy.check("apply_patch", comparison_probe)
    overwrite_denial = policy.check(
        "write_file", {"path": "frontend/app.js", "content": "replacement"}
    )
    if comparison_denial is None or overwrite_denial is None:
        raise RuntimeError("shared-file protection probe was unexpectedly admitted")
    syntax = subprocess.run(
        ["node", "--check", "app.js"], cwd=frontend, text=True, capture_output=True
    )
    policy.observe_validation(
        ok=syntax.returncode == 0,
        output=syntax.stdout + syntax.stderr,
        tool="node --check",
    )
    if syntax.returncode != 0:
        raise RuntimeError(syntax.stdout + syntax.stderr)
    return {
        "patch_decisions": decisions,
        "protected_comparison_probe": comparison_denial,
        "whole_file_overwrite_probe": overwrite_denial,
        "node_check": "ok",
    }


def repair_failed_candidate(
    *,
    workdir: Path,
    instruction: str,
    target_checks: list[dict[str, Any]],
    failed_evidence: dict[str, Any],
) -> dict[str, Any]:
    harness = workdir / ".harness"
    (harness / "browser_evidence_round_1.json").write_text(
        json.dumps(failed_evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    file_comm = FileComm(harness)
    passed, grades, stats = build_deterministic_failure_grades(
        file_comm=file_comm,
        round_num=1,
        sprint_num=1,
        sprint_context={"exit_criteria": [instruction]},
        ui_checks=target_checks,
        edit_guard=None,
    )
    if passed or grades.get("overall_passed") is not False:
        raise RuntimeError("failed browser evidence did not produce deterministic Repair grades")
    file_comm.write_grades(1, grades)
    packet = write_repair_packet(
        workdir=workdir,
        round_num=1,
        sprint_num=1,
        grades=grades,
    )
    if packet.get("status") != "repairable":
        raise RuntimeError("failed candidate did not produce an identifiable Repair packet")
    repair_plan = ensure_minimal_path_plan(
        workdir=workdir,
        harness_dir=harness,
        round_num=2,
        sprint_num=1,
        mode="repair",
        max_patch_lines=100,
        max_touched_files=2,
    )
    policy = MinimalPathPolicy.from_plan(workdir, repair_plan)
    relative = "frontend/app.js"
    target = workdir / relative
    policy.observe_result(
        "read_file",
        {"path": relative},
        ok=True,
        output=target.read_text(encoding="utf-8"),
    )
    repair_input = {
        "path": relative,
        "old_text": "itemsPerPage: 6,",
        "new_text": "itemsPerPage: 3,",
    }
    denial = policy.check("apply_patch", repair_input)
    if denial is not None:
        raise RuntimeError(f"evidence-driven Repair patch denied: {denial}")
    content = target.read_text(encoding="utf-8")
    if content.count(repair_input["old_text"]) != 1:
        raise RuntimeError("Repair patch no longer identifies one exact source occurrence")
    target.write_text(
        content.replace(repair_input["old_text"], repair_input["new_text"]),
        encoding="utf-8",
    )
    policy.observe_result("apply_patch", repair_input, ok=True, output="applied")
    syntax = subprocess.run(
        ["node", "--check", "app.js"],
        cwd=workdir / "frontend",
        text=True,
        capture_output=True,
    )
    policy.observe_validation(
        ok=syntax.returncode == 0,
        output=syntax.stdout + syntax.stderr,
        tool="node --check",
    )
    if syntax.returncode != 0:
        raise RuntimeError(syntax.stdout + syntax.stderr)
    return {
        "source_round": 1,
        "repair_round": 2,
        "grades": grades,
        "repair_packet": packet,
        "repair_plan": repair_plan,
        "repair_patch": repair_input,
        "policy_denial": denial,
        "evaluator_cost_usd": stats.cost_usd,
        "llm_evaluator_called": grades.get("evidence_route", {}).get(
            "llm_evaluator_called"
        ),
    }


async def main() -> int:
    args = parser().parse_args()
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = args.output_root.resolve() / f"webcompass_pagination_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "command": [sys.executable, *sys.argv],
        "python": platform.python_version(),
        "dataset": str(args.dataset.resolve()),
        "instance_id": args.instance_id,
        "task_type": args.task_type,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    row, patches = load_case(args.dataset, args.instance_id, args.task_type)
    workdir = run_dir / "workdir"
    materialize_source(row, workdir)
    before_hashes = file_hashes(workdir / "frontend")
    checks = browser_checks()
    instruction = next(
        (
            str(item["description"])
            for item in row.get("instruction", {}).get("description") or []
            if item.get("task_type") == args.task_type
        ),
        DEFAULT_DESCRIPTION,
    )
    target_checks = [
        check for check in checks if check.get("route") == "/discovery.html"
    ]
    artifacts = build_harness_artifacts(workdir, instruction, target_checks)
    source_evidence = await observe_site(
        workdir / "frontend", source_failure_check(), run_dir / "source_evidence.json"
    )
    authorization = apply_authorized_patches(workdir, artifacts["plan"], patches)
    after_hashes = file_hashes(workdir / "frontend")
    changed = sorted(
        path for path, digest in before_hashes.items() if after_hashes.get(path) != digest
    )
    expected_changed = sorted({str(item["path"]) for item in patches})
    protected_unchanged = all(
        after_hashes.get(path) == digest
        for path, digest in before_hashes.items()
        if path not in expected_changed
    )
    failed_edit_evidence = await observe_site(
        workdir / "frontend", checks, run_dir / "failed_edit_evidence.json"
    )
    failed_diff = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=workdir / "frontend",
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    (run_dir / "failed_edit.diff").write_text(failed_diff, encoding="utf-8")
    repair = repair_failed_candidate(
        workdir=workdir,
        instruction=instruction,
        target_checks=target_checks,
        failed_evidence=failed_edit_evidence,
    )
    accepted_evidence = await observe_site(
        workdir / "frontend", checks, run_dir / "accepted_evidence.json"
    )
    after_hashes = file_hashes(workdir / "frontend")
    changed = sorted(
        path for path, digest in before_hashes.items() if after_hashes.get(path) != digest
    )
    protected_unchanged = all(
        after_hashes.get(path) == digest
        for path, digest in before_hashes.items()
        if path not in expected_changed
    )
    source_statuses = [item.get("status") for item in source_evidence.get("checks") or []]
    failed_edit_statuses = [
        item.get("status") for item in failed_edit_evidence.get("checks") or []
    ]
    accepted_statuses = [
        item.get("status") for item in accepted_evidence.get("checks") or []
    ]
    guarded = artifacts["plan"].get("source_change_cone", {}).get("guarded_shared_regions") or []
    baseline_app = subprocess.run(
        ["git", "show", f"{artifacts['contract']['baseline_commit']}:app.js"],
        cwd=workdir / "frontend",
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    shared_region_error = MinimalPathPolicy.from_plan(
        workdir, artifacts["plan"]
    ).validate_guarded_shared_file(
        "frontend/app.js",
        before=baseline_app,
        after=(workdir / "frontend/app.js").read_text(encoding="utf-8"),
    )
    passed = (
        source_statuses == ["action_failed"]
        and "action_failed" in failed_edit_statuses
        and accepted_statuses
        and all(status == "ok" for status in accepted_statuses)
        and changed == expected_changed
        and protected_unchanged
        and shared_region_error is None
        and any(
            item.get("path") == "frontend/app.js" and item.get("symbol") == "Discovery"
            for item in guarded
        )
    )
    diff = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=workdir / "frontend",
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    (run_dir / "accepted_edit.diff").write_text(diff, encoding="utf-8")
    summary = {
        "status": "ok" if passed else "error",
        "instance_id": args.instance_id,
        "benchmark_alignment": row.get("metadata", {}).get("benchmark_alignment"),
        "page_scope": row.get("metadata", {}).get("page_scope"),
        "instruction_delta": instruction,
        "task_type": args.task_type,
        "round_policy": {
            "edit_max_rounds": 10,
            "rounds_used": 2,
            "stopped_early_on_accept": True,
        },
        "llm_calls": 0,
        "llm_cost_usd": 0,
        "source_evidence_statuses": source_statuses,
        "failed_edit_evidence_statuses": failed_edit_statuses,
        "accepted_evidence_statuses": accepted_statuses,
        "expected_changed_paths": expected_changed,
        "observed_changed_paths": changed,
        "protected_files_unchanged": protected_unchanged,
        "guarded_shared_regions": guarded,
        "final_shared_region_validation": {
            "status": "ok" if shared_region_error is None else "error",
            "error": shared_region_error,
        },
        "authorization": authorization,
        "natural_repair": repair,
        "artifacts": {
            "edit_card": str(workdir / ".harness/edit_card.json"),
            "minimal_path_plan": str(workdir / ".harness/minimal_path_plan_round_1.json"),
            "minimal_path_ledger": str(workdir / ".harness/minimal_path_ledger_round_1.jsonl"),
            "source_evidence": str(run_dir / "source_evidence.json"),
            "failed_edit_evidence": str(run_dir / "failed_edit_evidence.json"),
            "failed_edit_diff": str(run_dir / "failed_edit.diff"),
            "repair_packet": str(workdir / ".harness/repair_packet_round_1.json"),
            "accepted_evidence": str(run_dir / "accepted_evidence.json"),
            "diff": str(run_dir / "accepted_edit.diff"),
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": summary["status"], "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
