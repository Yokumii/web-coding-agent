"""Run real-browser minimality certificates on fixed historical trajectories.

This calibration never edits frontend source or historical result rows.  It
adds new certificate artifacts and appends one status-bearing row per
certificate to a dedicated calibration JSONL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from src.config import HarnessConfig
from src.orchestration.edit_dom_guard import sprint_baseline_name
from src.orchestration.minimality_runtime import (
    certify_round_minimality,
    ensure_minimality_policy,
)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_once(path: Path, payload: Any) -> None:
    if path.exists():
        existing = _read_json(path, None)
        if existing != payload:
            raise ValueError(f"refusing to overwrite a different artifact: {path}")
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _checks_for_sprint(run_dir: Path, sprint_num: int) -> list[dict[str, Any]]:
    plan = _read_json(run_dir / ".harness" / "ui_verification_plan.json", {})
    for sprint in plan.get("sprints") or []:
        if int(sprint.get("sprint") or 0) == sprint_num:
            return [item for item in sprint.get("checks") or [] if isinstance(item, dict)]
    return []


async def main_async(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    harness = run_dir / ".harness"
    if not (run_dir / "frontend" / ".git").exists():
        raise ValueError(f"frontend Git repository is missing: {run_dir / 'frontend'}")
    config = HarnessConfig(
        playwright_headless=True,
        minimality_guard_enabled=True,
        minimality_max_atoms=args.max_atoms,
        minimality_oracle_timeout_seconds=args.timeout,
    )
    expected_kinds = ["edit"] + (["repair"] if args.mode == "repair" else [])
    existing_case_ids: set[str] = set()
    if args.output_jsonl.is_file():
        for line in args.output_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("case_id"):
                existing_case_ids.add(str(row["case_id"]))
    expected_case_ids = {
        f"{run_dir.name}__round_{args.round}_{kind}" for kind in expected_kinds
    }
    if expected_case_ids.issubset(existing_case_ids):
        print(json.dumps({
            "status": "skipped", "reason": "already_recorded",
            "case_ids": sorted(expected_case_ids),
        }, ensure_ascii=False))
        return 0
    ensure_minimality_policy(harness, config)
    build_map = {
        str(args.round): {
            "round": args.round,
            "sprint": args.sprint,
            "mode": args.mode,
            "source_commit": args.round_source,
            "destination_commit": args.destination,
        }
    }
    if args.edit_source != args.round_source:
        # The earliest entry anchors final edit minimality; the current entry
        # separately anchors the repair delta.
        build_map[str(args.round - 1)] = {
            "round": args.round - 1,
            "sprint": args.sprint,
            "mode": "generate",
            "source_commit": args.edit_source,
            "destination_commit": args.round_source,
        }
    _write_json_once(harness / "round_build_map.json", build_map)
    sprint_baseline = harness / sprint_baseline_name(args.sprint)
    if not sprint_baseline.exists():
        shutil.copy2(harness / "edit_dom_baseline.json", sprint_baseline)

    result = await certify_round_minimality(
        run_dir=run_dir,
        config=config,
        round_num=args.round,
        sprint_num=args.sprint,
        checks=_checks_for_sprint(run_dir, args.sprint),
    )
    if result is None:
        raise RuntimeError("minimality policy unexpectedly disabled")
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("a", encoding="utf-8") as stream:
        for kind, certificate in (result.get("certificates") or {}).items():
            case_id = f"{run_dir.name}__round_{args.round}_{kind}"
            if case_id in existing_case_ids:
                continue
            row = {
                "status": "ok" if certificate.get("status") == "certified" else "rejected",
                "case_id": case_id,
                "source_run": str(run_dir),
                "kind": kind,
                "certificate_status": certificate.get("status"),
                "reason": certificate.get("reason"),
                "source_commit": certificate.get("source_commit"),
                "destination_commit": certificate.get("destination_commit"),
                "atom_count": certificate.get("atom_count"),
                "minimal_atom_count": certificate.get("minimal_atom_count"),
                "redundant_change_ids": certificate.get("redundant_change_ids", []),
                "certificate_artifact": str(
                    harness / f"minimality_round_{args.round}_{kind}.json"
                ),
                "evidence_mode": "real_chromium_dom_aria_and_action_contract",
            }
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps(row, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--sprint", type=int, default=1)
    parser.add_argument("--mode", choices=("generate", "repair"), required=True)
    parser.add_argument("--edit-source", required=True)
    parser.add_argument("--round-source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--max-atoms", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
