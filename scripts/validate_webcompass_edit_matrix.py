from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.minimal_patch_guard import (
    OracleOutcome,
    apply_atomic_patches,
    build_atomic_patches,
    certify_patch_minimality,
)
from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    ensure_minimal_path_plan,
)
from scripts.validate_webcompass_edit_case import file_hashes, materialize_source, observe_site


@dataclass(frozen=True)
class MatrixCase:
    instance_id: str
    name: str
    target_routes: tuple[str, ...]
    target_checks: tuple[dict[str, Any], ...]
    protected_checks: tuple[dict[str, Any], ...] = ()
    certify_minimality: bool = False
    patch_variant: str = "ground_truth"
    css_replacement_overrides: tuple[tuple[str, str], ...] = ()
    normalize_replacement_whitespace: bool = False


def _ticket(index: int) -> dict[str, Any]:
    return {
        "confirmationId": f"TKT-{index}",
        "title": f"Show {index}",
        "date": "2026-08-28",
        "time": "19:00",
        "quantity": 1,
    }


def _program(index: int) -> dict[str, Any]:
    return {
        "id": f"p{index}",
        "title": f"Program {index}",
        "time": "09:00",
        "duration": 60,
        "status": "ready",
        "location": "Hall A",
    }


def _log(index: int) -> dict[str, Any]:
    return {
        "timestamp": f"10:0{index}",
        "category": "Program",
        "action": f"Updated program {index}",
        "impact": "Ready",
    }


def _published_card(index: int) -> dict[str, Any]:
    return {
        "id": f"card-{index}",
        "headline": f"Published story {index}",
        "category": "council",
        "deadline": f"2026-08-{index + 10:02d}",
        "reporter": "Reporter",
        "verified": True,
        "checklist": {
            "sources": True,
            "documents": True,
            "interviews": True,
            "factCheck": True,
        },
    }


def _shift_log(index: int) -> dict[str, Any]:
    return {
        "id": index,
        "type": "general",
        "text": f"Shift event {index}",
        "timestamp": f"0{index}:00 AM",
    }


MATRIX_CASES: tuple[MatrixCase, ...] = (
    MatrixCase(
        instance_id="gen14-artifactsbench-ab-2-00341-f922414943",
        name="shared-file-discovery",
        target_routes=("/discovery.html",),
        target_checks=(
            {
                "id": "DISCOVERY-PAGINATION",
                "route": "/discovery.html",
                "category": "state",
                "actions": [
                    {"action": "assert_visible", "selector": "#pagination-controls"},
                    {"action": "assert_text", "selector": "#current-page", "value": "1"},
                    {
                        "action": "assert_property",
                        "selector": "#next-page",
                        "name": "disabled",
                        "value": False,
                    },
                ],
            },
        ),
        protected_checks=(
            {
                "id": "DISCOVERY-PROTECTED-COMPARISON",
                "route": "/comparison.html",
                "category": "regression",
                "actions": [
                    {"action": "assert_text", "selector": "main h1", "value": "Facility Comparison"},
                    {"action": "assert_no_console_errors"},
                ],
            },
        ),
    ),
    MatrixCase(
        instance_id="gen14-artifactsbench-ab-2-00363-905b3a5691",
        name="inline-summary-state-fixture",
        target_routes=("/summary.html",),
        target_checks=(
            {
                "id": "SUMMARY-PAGINATION",
                "route": "/summary.html",
                "category": "state",
                "actions": [
                    {
                        "action": "set_storage_value",
                        "storage": "local",
                        "key": "museum_app_state",
                        "value": {
                            "guides": [],
                            "tours": [],
                            "requests": [],
                            "incidents": [
                                {
                                    "timestamp": 1_700_000_000_000 + index,
                                    "category": "Safety",
                                    "location": f"Hall {index}",
                                }
                                for index in range(6)
                            ],
                        },
                        "encoding": "json",
                    },
                    {"action": "reload"},
                    {"action": "assert_text", "selector": "#page-indicator", "value": "Page 1 of 2"},
                    {"action": "assert_count", "selector": "#activity-log .log-entry", "count": 5},
                    {"action": "click", "selector": "#pagination-controls button:last-child"},
                    {"action": "assert_text", "selector": "#page-indicator", "value": "Page 2 of 2"},
                    {"action": "assert_count", "selector": "#activity-log .log-entry", "count": 1},
                ],
            },
        ),
        protected_checks=(
            {
                "id": "SUMMARY-PROTECTED-DASHBOARD",
                "route": "/index.html",
                "category": "regression",
                "actions": [{"action": "assert_visible", "selector": ".brand"}],
            },
        ),
    ),
    MatrixCase(
        instance_id="gen14-artifactsbench-ab-2-00277-9344a0ec73",
        name="route-local-tickets-nonminimal",
        target_routes=("/mytickets.html",),
        target_checks=(
            {
                "id": "TICKETS-PAGINATION",
                "route": "/mytickets.html",
                "category": "state",
                "actions": [
                    {
                        "action": "set_storage_value",
                        "storage": "local",
                        "key": "starlight_tickets",
                        "value": [_ticket(index) for index in range(1, 5)],
                        "encoding": "json",
                    },
                    {"action": "reload"},
                    {"action": "assert_count", "selector": ".ticket-card", "count": 3},
                    {"action": "assert_text", "selector": ".pagination-controls span", "value": "Page 1 of 2"},
                    {"action": "click", "selector": ".page-next"},
                    {"action": "assert_count", "selector": ".ticket-card", "count": 1},
                    {"action": "assert_text", "selector": ".pagination-controls span", "value": "Page 2 of 2"},
                ],
            },
        ),
        protected_checks=(
            {
                "id": "TICKETS-PROTECTED-NAV",
                "route": "/index.html",
                "category": "regression",
                "actions": [{"action": "assert_text", "selector": ".logo", "value": "🎭 Starlight Theater"}],
            },
        ),
        certify_minimality=True,
    ),
    MatrixCase(
        instance_id="gen14-artifactsbench-ab-2-00328-0f9adabee0",
        name="two-route-shared-state",
        target_routes=("/", "/report.html"),
        target_checks=(
            {
                "id": "DASHBOARD-PAGINATION",
                "route": "/",
                "category": "state",
                "actions": [
                    {
                        "action": "set_storage_value",
                        "storage": "local",
                        "key": "museum_ops_state",
                        "value": {
                            "programs": [_program(index) for index in range(1, 6)],
                            "logs": [_log(index) for index in range(1, 6)],
                            "dashboardPage": 1,
                            "reportPage": 1,
                            "pageSize": 4,
                        },
                        "encoding": "json",
                    },
                    {"action": "reload"},
                    {"action": "assert_text", "selector": "#dashPageInfo", "value": "Page 1 of 2"},
                    {"action": "assert_count", "selector": ".program-card", "count": 4},
                    {"action": "click", "selector": "#nextDashPage"},
                    {"action": "assert_text", "selector": "#dashPageInfo", "value": "Page 2 of 2"},
                    {"action": "assert_count", "selector": ".program-card", "count": 1},
                ],
            },
            {
                "id": "REPORT-PAGINATION",
                "route": "/report.html",
                "category": "persistence",
                "actions": [
                    {"action": "assert_text", "selector": "#reportPageInfo", "value": "Page 1 of 2"},
                    {"action": "assert_count", "selector": "#logBody tr", "count": 4},
                    {"action": "click", "selector": "#nextReportPage"},
                    {"action": "assert_text", "selector": "#reportPageInfo", "value": "Page 2 of 2"},
                    {"action": "assert_count", "selector": "#logBody tr", "count": 1},
                ],
            },
        ),
        protected_checks=(
            {
                "id": "SHARED-STATE-PROTECTED-STAFF",
                "route": "/staff.html",
                "category": "regression",
                "actions": [{"action": "assert_visible", "selector": ".sidebar"}],
            },
        ),
    ),
    MatrixCase(
        instance_id="gen14-artifactsbench-ab-2-00164-ce3dab4f11",
        name="multi-html-log-scoped-css",
        target_routes=("/log.html",),
        target_checks=(
            {
                "id": "LOG-PAGINATION-SCOPED-STYLE",
                "route": "/log.html",
                "category": "state",
                "actions": [
                    {
                        "action": "set_storage_value",
                        "storage": "local",
                        "key": "transit_hub_state_v1",
                        "value": {
                            "vehicles": [],
                            "dispatches": [],
                            "alerts": [],
                            "logs": [_shift_log(index) for index in range(1, 8)],
                        },
                        "encoding": "json",
                    },
                    {"action": "reload"},
                    {
                        "action": "assert_computed_style",
                        "selector": "#log-pagination",
                        "property": "display",
                        "value": "flex",
                    },
                    {
                        "action": "assert_count",
                        "selector": "#log-list .log-item",
                        "count": 5,
                    },
                    {"action": "click", "selector": "#next-page"},
                    {
                        "action": "assert_text",
                        "selector": "#page-indicator",
                        "value": "Page 2 of 2",
                    },
                    {
                        "action": "assert_count",
                        "selector": "#log-list .log-item",
                        "count": 2,
                    },
                ],
            },
        ),
        protected_checks=(
            {
                "id": "LOG-PROTECTED-DASHBOARD",
                "route": "/index.html",
                "category": "regression",
                "actions": [
                    {"action": "assert_text", "selector": "main h1", "value": "Fleet Overview"},
                    {"action": "assert_no_console_errors"},
                ],
            },
        ),
        patch_variant="controlled_target_scoped_css_repair",
        normalize_replacement_whitespace=True,
        css_replacement_overrides=(
            (
                "styles.css",
                ".timeline-list { display: flex; flex-direction: column; gap: 1rem; "
                "max-height: calc(100vh - 200px); overflow-y: auto; padding-right: 0.5rem; }\n"
                "#log-list { max-height: calc(100vh - 260px); }\n"
                "#log-pagination.pagination-controls { display: flex; justify-content: center; "
                "align-items: center; gap: 1rem; padding-top: 1rem; "
                "border-top: 1px solid var(--border); margin-top: 1rem; }\n"
                "#log-pagination .page-info { font-weight: 600; color: var(--text-muted); "
                "font-size: 0.9rem; }",
            ),
        ),
    ),
    MatrixCase(
        instance_id="gen14-webcompass-wc-5-00404-e13db617c3",
        name="hash-router-report",
        target_routes=("/#/report",),
        target_checks=(
            {
                "id": "HASH-REPORT-PAGINATION-INITIAL",
                "route": "/#/report",
                "category": "state",
                "actions": [
                    {
                        "action": "set_storage_value",
                        "storage": "local",
                        "key": "csb_state",
                        "value": {"cards": [_published_card(index) for index in range(1, 7)]},
                        "encoding": "json",
                    },
                    {"action": "reload"},
                    {"action": "assert_hash", "value": "#/report"},
                    {"action": "assert_visible", "selector": "#report-pagination"},
                    {
                        "action": "assert_computed_style",
                        "selector": "#report-pagination",
                        "property": "display",
                        "value": "flex",
                    },
                    {"action": "assert_count", "selector": ".recent-item", "count": 3},
                ],
            },
            {
                "id": "HASH-REPORT-PAGINATION-NEXT",
                "route": "/#/report",
                "category": "state",
                "actions": [
                    {"action": "click", "selector": "[data-page-action='next']"},
                    {"action": "assert_text", "selector": ".page-indicator", "value": "Page 2 of 2"},
                    {"action": "assert_count", "selector": ".recent-item", "count": 3},
                ],
            },
        ),
    ),
)


def parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parents[1]
    value = argparse.ArgumentParser(
        description="Run a zero-LLM real multi-page/multi-file Edit harness matrix."
    )
    value.add_argument(
        "--dataset",
        type=Path,
        default=repo.parent
        / "WebCoding_Data/output/0805_supplement_release_cache/text-edit.jsonl.gz",
    )
    value.add_argument(
        "--output-root", type=Path, default=repo / "logs/edit_matrix_20260828"
    )
    value.add_argument("--case", action="append", default=[])
    return value


def load_rows(dataset: Path, instance_ids: set[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with gzip.open(dataset, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            instance_id = str(row.get("instance_id", ""))
            if instance_id in instance_ids:
                rows[instance_id] = row
    missing = sorted(instance_ids - set(rows))
    if missing:
        raise ValueError("matrix instances not found: " + ", ".join(missing))
    return rows


def pagination_patches(row: dict[str, Any]) -> list[dict[str, str]]:
    return [
        item
        for item in row.get("response") or []
        if isinstance(item, dict) and item.get("task_type") == "Pagination"
    ]


def case_patches(row: dict[str, Any], case: MatrixCase) -> list[dict[str, str]]:
    overrides = dict(case.css_replacement_overrides)
    output: list[dict[str, str]] = []
    for source_patch in pagination_patches(row):
        patch = {key: str(value) for key, value in source_patch.items()}
        if patch["path"] in overrides:
            patch["replace"] = overrides[patch["path"]]
        if case.normalize_replacement_whitespace:
            patch["replace"] = re.sub(r"[ \t]+(?=\r?$)", "", patch["replace"], flags=re.M)
        output.append(patch)
    return output


def source_destination_maps(
    row: dict[str, Any], patches: list[dict[str, str]]
) -> tuple[dict[str, str], dict[str, str]]:
    source = {
        str(item["path"]): str(item["code"])
        for item in row.get("instruction", {}).get("src_code") or []
    }
    destination = dict(source)
    for patch in patches:
        path = str(patch["path"])
        search = str(patch["search"])
        replace = str(patch["replace"])
        if destination[path].count(search) != 1:
            raise ValueError(f"non-unique ground-truth patch: {path}")
        destination[path] = destination[path].replace(search, replace, 1)
    return source, destination


def write_code_map(frontend: Path, code: dict[str, str]) -> None:
    frontend.mkdir(parents=True, exist_ok=True)
    for relative, content in code.items():
        target = frontend / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _validate_policy(policy: MinimalPathPolicy, frontend: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "diff", "--check", "--", "."],
        cwd=frontend,
        text=True,
        capture_output=True,
    )
    policy.observe_validation(
        ok=result.returncode == 0,
        output=result.stdout + result.stderr,
        tool="git diff --check",
    )
    return {
        "ok": result.returncode == 0,
        "tool": "git diff --check",
        "output": result.stdout + result.stderr,
    }


def apply_with_policy(
    workdir: Path, plan: dict[str, Any], patches: list[dict[str, str]]
) -> dict[str, Any]:
    frontend = workdir / "frontend"
    policy = MinimalPathPolicy.from_plan(workdir, plan)
    cone = plan.get("source_change_cone") or {}
    order = list(
        dict.fromkeys(
            [
                *(cone.get("initial_paths") or []),
                *(cone.get("local_paths") or []),
                *(cone.get("dependency_paths") or []),
            ]
        )
    )
    ranks = {path: index for index, path in enumerate(order)}
    ordered = sorted(
        enumerate(patches),
        key=lambda item: (ranks.get(f"frontend/{item[1]['path']}", 10_000), item[0]),
    )
    records: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    active_path: str | None = None
    observed: set[str] = set()
    for relative in cone.get("initial_paths") or []:
        target = workdir / str(relative)
        if not target.is_file():
            continue
        policy.observe_result(
            "read_file",
            {"path": str(relative)},
            ok=True,
            output=target.read_text(encoding="utf-8"),
        )
        observed.add(str(relative))
    for original_index, patch in ordered:
        relative = f"frontend/{patch['path']}"
        if active_path is not None and active_path != relative:
            validations.append(_validate_policy(policy, frontend))
        target = workdir / relative
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
            "old_text": str(patch["search"]),
            "new_text": str(patch["replace"]),
        }
        denial = policy.check("apply_patch", tool_input)
        record = {
            "patch_index": original_index,
            "path": relative,
            "allowed": denial is None,
            "denial": denial,
        }
        records.append(record)
        if denial is not None:
            continue
        content = target.read_text(encoding="utf-8")
        if content.count(tool_input["old_text"]) != 1:
            record["allowed"] = False
            record["denial"] = "ground-truth patch is not uniquely replayable"
            continue
        target.write_text(
            content.replace(tool_input["old_text"], tool_input["new_text"], 1),
            encoding="utf-8",
        )
        policy.observe_result("apply_patch", tool_input, ok=True, output="applied")
        active_path = relative
    if active_path is not None:
        validations.append(_validate_policy(policy, frontend))
    return {
        "patches": records,
        "validations": validations,
        "validation_passed": bool(validations) and all(
            bool(item["ok"]) for item in validations
        ),
        "allowed": sum(bool(item["allowed"]) for item in records),
        "denied": sum(not bool(item["allowed"]) for item in records),
    }


def _status_list(evidence: dict[str, Any]) -> list[str]:
    return [str(item.get("status")) for item in evidence.get("checks") or []]


async def counterfactual_certificate(
    *,
    source: dict[str, str],
    destination: dict[str, str],
    case: MatrixCase,
    output_dir: Path,
) -> dict[str, Any]:
    atoms = build_atomic_patches(source, destination)
    attempt = 0

    async def oracle(kept: tuple[str, ...]) -> OracleOutcome:
        nonlocal attempt
        attempt += 1
        candidate_dir = output_dir / f"attempt_{attempt:02d}"
        try:
            candidate = apply_atomic_patches(source, atoms, set(kept))
            frontend = candidate_dir / "frontend"
            write_code_map(frontend, candidate)
            evidence = await observe_site(
                frontend,
                [*case.target_checks, *case.protected_checks],
                candidate_dir / "browser_evidence.json",
            )
            statuses = _status_list(evidence)
            target_count = len(case.target_checks)
            target_passed = bool(target_count) and all(
                status == "ok" for status in statuses[:target_count]
            )
            preservation_passed = all(
                status == "ok" for status in statuses[target_count:]
            )
            return OracleOutcome(
                status="ok" if target_passed and preservation_passed else "candidate_failed",
                target_passed=target_passed,
                preservation_passed=preservation_passed,
                evidence={
                    "attempt": attempt,
                    "statuses": statuses,
                    "path": str(candidate_dir / "browser_evidence.json"),
                },
            )
        except Exception as exc:
            return OracleOutcome(
                status="infrastructure_error",
                target_passed=False,
                preservation_passed=False,
                evidence={"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"},
            )

    certificate = await certify_patch_minimality(atoms, oracle, max_atoms=12)
    certificate["atomic_patches"] = [item.payload() for item in atoms]
    return certificate


async def run_case(
    *, case: MatrixCase, row: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    case_dir = run_dir / case.name
    case_dir.mkdir(parents=True)
    workdir = case_dir / "workdir"
    materialize_source(row, workdir)
    before_hashes = file_hashes(workdir / "frontend")
    patches = case_patches(row, case)
    source, destination = source_destination_maps(row, patches)
    prepare_edit_task_contract(workdir, requested_target_routes=case.target_routes)
    harness = workdir / ".harness"
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {"sprints": [{"sprint": 1, "checks": list(case.target_checks)}]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plan = ensure_minimal_path_plan(
        workdir=workdir,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=120,
        max_touched_files=6,
    )
    source_evidence = await observe_site(
        workdir / "frontend",
        list(case.target_checks),
        case_dir / "source_evidence.json",
    )
    authorization = apply_with_policy(workdir, plan, patches)
    destination_evidence = await observe_site(
        workdir / "frontend",
        [*case.target_checks, *case.protected_checks],
        case_dir / "destination_evidence.json",
    )
    after_hashes = file_hashes(workdir / "frontend")
    changed = sorted(
        path for path, digest in before_hashes.items() if after_hashes.get(path) != digest
    )
    denied_paths = sorted(
        {
            str(item["path"])
            for item in authorization["patches"]
            if not item["allowed"]
        }
    )
    behavior_paths = {
        f"frontend/{item['path']}"
        for item in patches
        if Path(str(item["path"])).suffix.lower() not in {".css", ".scss"}
    }
    behavior_denied = sorted(set(denied_paths) & behavior_paths)
    statuses = _status_list(destination_evidence)
    target_count = len(case.target_checks)
    target_passed = bool(target_count) and all(
        status == "ok" for status in statuses[:target_count]
    )
    protected_passed = all(status == "ok" for status in statuses[target_count:])
    certificate = None
    if case.certify_minimality:
        certificate = await counterfactual_certificate(
            source=source,
            destination=destination,
            case=case,
            output_dir=case_dir / "counterfactual",
        )
        (case_dir / "counterfactual_certificate.json").write_text(
            json.dumps(certificate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    mechanical_passed = bool(authorization.get("validation_passed"))
    status = (
        "ok"
        if target_passed and protected_passed and not behavior_denied and mechanical_passed
        else "rejected"
    )
    return {
        "status": status,
        "case": case.name,
        "instance_id": case.instance_id,
        "page_scope": row.get("metadata", {}).get("page_scope"),
        "patch_variant": case.patch_variant,
        "target_routes": list(case.target_routes),
        "plan_status": plan.get("status"),
        "route_scope": plan.get("route_scope"),
        "design_system_context": plan.get("design_system_context"),
        "guarded_shared_regions": (
            plan.get("source_change_cone", {}).get("guarded_shared_regions") or []
        ),
        "source_statuses": _status_list(source_evidence),
        "destination_statuses": statuses,
        "target_passed": target_passed,
        "protected_passed": protected_passed,
        "authorization": authorization,
        "mechanical_validation_passed": mechanical_passed,
        "denied_paths": denied_paths,
        "behavior_denied_paths": behavior_denied,
        "observed_changed_paths": changed,
        "counterfactual_certificate": certificate,
        "llm_calls": 0,
        "llm_cost_usd": 0,
    }


async def main() -> int:
    args = parser().parse_args()
    selected_names = set(args.case)
    selected = [
        case for case in MATRIX_CASES if not selected_names or case.name in selected_names
    ]
    unknown = sorted(selected_names - {case.name for case in MATRIX_CASES})
    if unknown:
        raise ValueError("unknown matrix cases: " + ", ".join(unknown))
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = args.output_root.resolve() / f"real_edit_matrix_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "command": [sys.executable, *sys.argv],
        "python": platform.python_version(),
        "dataset": str(args.dataset.resolve()),
        "cases": [case.name for case in selected],
        "llm_calls": 0,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = load_rows(args.dataset, {case.instance_id for case in selected})
    records_path = run_dir / "records.jsonl"
    records: list[dict[str, Any]] = []
    for case in selected:
        try:
            record = await run_case(case=case, row=rows[case.instance_id], run_dir=run_dir)
        except Exception as exc:
            record = {
                "status": "error",
                "case": case.name,
                "instance_id": case.instance_id,
                "error": f"{type(exc).__name__}: {exc}",
                "llm_calls": 0,
                "llm_cost_usd": 0,
            }
        records.append(record)
        with records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    summary = {
        "status": "ok" if all(item["status"] != "error" for item in records) else "error",
        "run_dir": str(run_dir),
        "counts": {
            key: sum(item["status"] == key for item in records)
            for key in ("ok", "rejected", "error")
        },
        "llm_calls": 0,
        "llm_cost_usd": 0,
        "records": [
            {
                "case": item["case"],
                "status": item["status"],
                "target_passed": item.get("target_passed"),
                "denied_paths": item.get("denied_paths", []),
                "certificate_status": (
                    (item.get("counterfactual_certificate") or {}).get("status")
                ),
            }
            for item in records
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
