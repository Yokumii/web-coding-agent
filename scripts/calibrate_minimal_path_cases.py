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
    state_name,
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
        if row.get("case_id") and row.get("status") in {"ok", "rejected"}:
            output.add(str(row["case_id"]))
    return output


def _write_once(path: Path, payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.is_file():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError(
                f"refusing to overwrite different calibration artifact: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


async def _validate_checkpoint(
    *,
    shadow: Path,
    harness: Path,
    config: HarnessConfig,
    round_num: int,
) -> tuple[bool, dict[str, Any]]:
    """Render the current real source and retain semantic validation evidence."""
    try:
        stack = await start_app_stack(shadow, harness, config, round_num=round_num)
        try:
            snapshot = await snapshot_semantic_dom(stack.frontend_url, headless=True)
        finally:
            await stack.close()
    except Exception as exc:
        return False, {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }
    return True, {
        "status": "ok",
        "root_count": len(snapshot.get("roots", [])),
        "root_keys": [
            str(item.get("key"))
            for item in snapshot.get("roots", [])
            if isinstance(item, dict) and item.get("key")
        ],
    }


def _patch_order(plan: dict[str, Any], patches: list[Any]) -> list[Any]:
    """Replay an unordered historical diff in controller-guided path order."""
    cone = plan.get("source_change_cone") or {}
    initial = [str(item) for item in cone.get("initial_paths") or []]
    edges = [
        (str(item.get("from")), str(item.get("to")))
        for item in cone.get("dependency_edges") or []
        if isinstance(item, dict) and item.get("from") and item.get("to")
    ]
    depths = {path: 0 for path in initial}
    changed = True
    while changed:
        changed = False
        for source, target in edges:
            if source in depths and (
                target not in depths or depths[target] > depths[source] + 1
            ):
                depths[target] = depths[source] + 1
                changed = True
            if target in depths and (
                source not in depths or depths[source] > depths[target] + 1
            ):
                depths[source] = depths[target] + 1
                changed = True
    return sorted(
        patches,
        key=lambda patch: (
            depths.get(f"frontend/{patch.path}", 10_000),
            str(patch.path),
            str(patch.change_id),
        ),
    )


async def calibrate(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    frontend_repo = run_dir / "frontend"
    case_id = (
        f"{run_dir.name}__round_{args.round}_{args.source[:7]}_{args.destination[:7]}"
    )
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
            baseline = await snapshot_semantic_dom(stack.frontend_url, headless=True)
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
        patches = _patch_order(
            plan, build_atomic_patches(source_code, destination_code)
        )
        decisions: list[dict[str, Any]] = []
        validation_evidence: list[dict[str, Any]] = []
        for patch in patches:
            tool_input = {
                "path": f"frontend/{patch.path}",
                "old_text": patch.search,
                "new_text": patch.replace,
            }
            target = frontend / patch.path
            read_output = (
                target.read_text(encoding="utf-8", errors="replace")
                if target.is_file()
                else "new source path"
            )
            policy.observe_result(
                "read_file", {"path": tool_input["path"]}, ok=True, output=read_output
            )
            denial = policy.check("apply_patch", tool_input)
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
            try:
                if patch.search:
                    content = target.read_text(encoding="utf-8", errors="replace")
                    target.write_text(
                        content.replace(patch.search, patch.replace, 1),
                        encoding="utf-8",
                    )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(patch.replace, encoding="utf-8")
            except Exception as exc:
                policy.observe_result(
                    "apply_patch", tool_input, ok=False, output=str(exc)
                )
                decisions[-1]["decision"] = "apply_error"
                decisions[-1]["reason"] = f"{type(exc).__name__}: {exc}"
                continue
            policy.observe_result(
                "apply_patch", tool_input, ok=True, output="historical patch applied"
            )
            validation_ok, validation = await _validate_checkpoint(
                shadow=shadow,
                harness=harness,
                config=config,
                round_num=args.round,
            )
            validation.update(
                {
                    "after_change_id": patch.change_id,
                    "path": patch.path,
                }
            )
            validation_evidence.append(validation)
            policy.observe_validation(
                ok=validation_ok,
                output=validation,
                tool="harness_browser_semantic_dom",
            )

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
        _write_once(artifact_dir / "validation_evidence.json", validation_evidence)
        _write_once(
            artifact_dir / state_name(args.round),
            _read_json(harness / state_name(args.round), {}),
        )
        all_patches_applied = all(item["decision"] == "allow" for item in decisions)
        final_validation_ok = bool(
            validation_evidence and validation_evidence[-1].get("status") == "ok"
        )
        row = {
            "status": (
                "ok" if all_patches_applied and final_validation_ok else "rejected"
            ),
            "case_id": case_id,
            "source_run": str(run_dir),
            "source_commit": args.source,
            "destination_commit": args.destination,
            "kind": args.kind,
            "evidence_mode": (
                "real_source_commit_progressive_tool_policy_real_chromium_semantic_dom"
            ),
            "plan_status": plan.get("status"),
            "local_paths": plan["source_change_cone"]["local_paths"],
            "dependency_paths": plan["source_change_cone"]["dependency_paths"],
            "protected_path_count": len(plan["source_change_cone"]["protected_paths"]),
            "allowed_root_keys": plan["dom_change_cone"]["allowed_root_keys"],
            "allow_new_roots": plan["dom_change_cone"]["allow_new_roots"],
            "patch_count": len(patches),
            "allowed_patch_count": sum(
                item["decision"] == "allow" for item in decisions
            ),
            "denied_patch_count": sum(item["decision"] == "deny" for item in decisions),
            "validation_checkpoint_count": len(validation_evidence),
            "successful_validation_checkpoint_count": sum(
                item.get("status") == "ok" for item in validation_evidence
            ),
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
    row: dict[str, Any] | None = None
    last_failure: tuple[str, str] | None = None
    for attempt in range(1, args.max_attempts + 1):
        try:
            row = await asyncio.wait_for(
                calibrate(args), timeout=float(args.case_timeout)
            )
            row["attempt_count"] = attempt
            break
        except TimeoutError:
            last_failure = (
                "timeout",
                f"case exceeded {args.case_timeout:.0f}s hard timeout",
            )
        except Exception as exc:
            last_failure = (
                "error",
                f"{type(exc).__name__}: {str(exc)[:1000]}",
            )
    if row is None:
        status, error = last_failure or ("error", "unknown calibration failure")
        row = {
            "status": status,
            "case_id": case_id,
            "source_run": str(args.run_dir.resolve()),
            "source_commit": args.source,
            "destination_commit": args.destination,
            "kind": args.kind,
            "attempt_count": args.max_attempts,
            "error": error,
        }
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
    print(json.dumps(row, ensure_ascii=False))
    return 0 if row["status"] in {"ok", "rejected"} else 1


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
    parser.add_argument("--case-timeout", type=float, default=900)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
