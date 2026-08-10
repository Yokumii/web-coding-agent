"""Replay the online minimal-path policy on real historical edit trajectories.

Historical runs stay read-only.  Their source commit is materialized in an
isolated directory, rendered in Chromium to obtain a v2 semantic-anchor frame,
then fed through the same planning and mutation policy used by new runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from src.config import HarnessConfig
from src.orchestration.edit_dom_guard import snapshot_semantic_dom
from src.orchestration.minimal_patch_guard import build_atomic_patches
from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    ensure_minimal_path_plan,
    ledger_name,
    plan_name,
)
from src.orchestration.minimality_runtime import (
    _code_at_commit,
    _safe_extract_git_archive,
)
from src.orchestration.runtime import start_app_stack


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _existing_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    output: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("case_id"):
            output.add(str(row["case_id"]))
    return output


def _write_once(path: Path, payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.is_file():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError(f"refusing to overwrite different calibration artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


async def calibrate(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    frontend_repo = run_dir / "frontend"
    case_id = f"{run_dir.name}__round_{args.round}_{args.source[:7]}_{args.destination[:7]}"
    artifact_dir = args.artifact_dir.resolve() / case_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="minimal_path_case_") as raw:
        shadow = Path(raw)
        frontend = shadow / "frontend"
        frontend.mkdir()
        _safe_extract_git_archive(frontend_repo, args.source, frontend)
        harness = shadow / ".harness"
        harness.mkdir()
        shutil.copy2(
            run_dir / ".harness" / "ui_verification_plan.json",
            harness / "ui_verification_plan.json",
        )
        config = HarnessConfig(
            frontend_port=args.port,
            playwright_headless=True,
            minimal_path_guidance_enabled=True,
            minimal_path_max_patch_lines=args.max_patch_lines,
            minimal_path_max_touched_files=args.max_touched_files,
        )
        stack = await start_app_stack(shadow, harness, config, round_num=args.round)
        try:
            baseline = await snapshot_semantic_dom(
                stack.frontend_url, headless=True
            )
        finally:
            await stack.close()
        (harness / f"edit_dom_source_sprint_{args.sprint}.json").write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        plan = ensure_minimal_path_plan(
            workdir=shadow,
            harness_dir=harness,
            round_num=args.round,
            sprint_num=args.sprint,
            mode=args.mode,
            max_patch_lines=args.max_patch_lines,
            max_touched_files=args.max_touched_files,
        )
        policy = MinimalPathPolicy.from_plan(shadow, plan)
        source_code = _code_at_commit(frontend_repo, args.source)
        destination_code = _code_at_commit(frontend_repo, args.destination)
        patches = build_atomic_patches(source_code, destination_code)
        decisions: list[dict[str, Any]] = []
        for patch in patches:
            denial = policy.check(
                "apply_patch",
                {
                    "path": f"frontend/{patch.path}",
                    "old_text": patch.search,
                    "new_text": patch.replace,
                },
            )
            decisions.append(
                {
                    "change_id": patch.change_id,
                    "path": patch.path,
                    "decision": "deny" if denial else "allow",
                    "reason": denial,
                }
            )
            if denial:
                continue
            target = frontend / patch.path
            if patch.search:
                content = target.read_text(encoding="utf-8", errors="replace")
                target.write_text(
                    content.replace(patch.search, patch.replace, 1), encoding="utf-8"
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(patch.replace, encoding="utf-8")

        historical_certificate = _read_json(
            run_dir / ".harness" / f"minimality_round_{args.round}_{args.kind}.json",
            {},
        )
        ledger = [
            json.loads(line)
            for line in (harness / ledger_name(args.round))
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        _write_once(artifact_dir / plan_name(args.round), plan)
        _write_once(artifact_dir / "semantic_source_frame.json", baseline)
        _write_once(artifact_dir / "mutation_decisions.json", decisions)
        _write_once(artifact_dir / "mutation_ledger.json", ledger)
        row = {
            "status": "ok" if all(item["decision"] == "allow" for item in decisions) else "rejected",
            "case_id": case_id,
            "source_run": str(run_dir),
            "source_commit": args.source,
            "destination_commit": args.destination,
            "kind": args.kind,
            "evidence_mode": "real_source_commit_real_chromium_online_tool_policy",
            "plan_status": plan.get("status"),
            "local_paths": plan["source_change_cone"]["local_paths"],
            "dependency_paths": plan["source_change_cone"]["dependency_paths"],
            "protected_path_count": len(plan["source_change_cone"]["protected_paths"]),
            "allowed_root_keys": plan["dom_change_cone"]["allowed_root_keys"],
            "allow_new_roots": plan["dom_change_cone"]["allow_new_roots"],
            "patch_count": len(patches),
            "allowed_patch_count": sum(item["decision"] == "allow" for item in decisions),
            "denied_patch_count": sum(item["decision"] == "deny" for item in decisions),
            "historical_counterfactual_status": historical_certificate.get("status"),
            "historical_redundant_change_ids": historical_certificate.get(
                "redundant_change_ids", []
            ),
            "artifacts": str(artifact_dir),
        }
        return row


async def main_async(args: argparse.Namespace) -> int:
    case_id = (
        f"{args.run_dir.resolve().name}__round_{args.round}_"
        f"{args.source[:7]}_{args.destination[:7]}"
    )
    if case_id in _existing_ids(args.output_jsonl):
        print(json.dumps({"status": "skipped", "case_id": case_id}, ensure_ascii=False))
        return 0
    row = await calibrate(args)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("a", encoding="utf-8") as stream:
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
    parser.add_argument("--kind", choices=("edit", "repair"), required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-patch-lines", type=int, default=120)
    parser.add_argument("--max-touched-files", type=int, default=3)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
