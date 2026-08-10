"""Real-browser runtime for counterfactual edit/repair certification."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from src.config import HarnessConfig
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.edit_dom_guard import (
    compare_contract,
    repair_baseline_name,
    snapshot_semantic_dom,
    sprint_baseline_name,
)
from src.orchestration.minimal_patch_guard import (
    AtomicPatch,
    OracleOutcome,
    apply_atomic_patches,
    build_atomic_patches,
    certify_patch_minimality,
)
from src.orchestration.runtime import start_app_stack


POLICY_NAME = "minimality_policy.json"
BUILD_MAP_NAME = "round_build_map.json"
CODE_EXTENSIONS = {
    ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
    ".vue", ".svelte", ".svg", ".json", ".json5", ".qml", ".ets",
    ".wxml", ".wxss",
}
IGNORED_CHANGED_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(frontend: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args], cwd=frontend, check=True, capture_output=True,
        text=not binary,
    )
    return result.stdout


def _head(frontend: Path) -> str:
    return str(_git(frontend, "rev-parse", "HEAD")).strip()


def ensure_minimality_policy(harness_dir: Path, config: HarnessConfig) -> dict[str, Any]:
    path = harness_dir / POLICY_NAME
    existing = _read_json(path, None)
    if isinstance(existing, dict):
        return existing
    policy = {
        "schema_version": "minimality-policy-v1",
        "enabled": bool(config.minimality_guard_enabled),
        "max_atomic_changes": int(config.minimality_max_atoms),
        "oracle_timeout_seconds": int(config.minimality_oracle_timeout_seconds),
        "obligations": [
            "source_must_fail_target_contract",
            "destination_must_pass_target_contract",
            "protected_dom_aria_frame_must_pass",
            "every_surviving_patch_atom_must_be_counterfactually_necessary",
        ],
    }
    _write_json(path, policy)
    return policy


def record_round_build_source(
    harness_dir: Path, frontend: Path, *, round_num: int, sprint_num: int, mode: str
) -> str:
    path = harness_dir / BUILD_MAP_NAME
    payload = _read_json(path, {})
    key = str(round_num)
    if key not in payload:
        payload[key] = {
            "round": round_num,
            "sprint": sprint_num,
            "mode": mode,
            "source_commit": _head(frontend),
            "destination_commit": None,
        }
        _write_json(path, payload)
    return str(payload[key]["source_commit"])


def record_round_build_destination(
    harness_dir: Path, frontend: Path, *, round_num: int
) -> str:
    path = harness_dir / BUILD_MAP_NAME
    payload = _read_json(path, {})
    key = str(round_num)
    if key not in payload:
        raise ValueError(f"round {round_num} has no recorded build source")
    destination = _head(frontend)
    payload[key]["destination_commit"] = destination
    _write_json(path, payload)
    return destination


def _code_at_commit(frontend: Path, commit: str) -> dict[str, str]:
    paths = str(_git(frontend, "ls-tree", "-r", "--name-only", commit)).splitlines()
    output: dict[str, str] = {}
    for path in sorted(paths):
        if Path(path).suffix.lower() not in CODE_EXTENSIONS:
            continue
        output[path] = str(_git(frontend, "show", f"{commit}:{path}"))
    return output


def _changed_paths(frontend: Path, source: str, destination: str) -> list[str]:
    return sorted(
        line for line in str(
            _git(frontend, "diff", "--name-only", f"{source}..{destination}", "--")
        ).splitlines() if line
    )


def _safe_extract_git_archive(frontend: Path, commit: str, destination: Path) -> None:
    archive = _git(frontend, "archive", "--format=tar", commit, binary=True)
    assert isinstance(archive, bytes)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"unsafe git archive member: {member.name}")
            target = destination.joinpath(*pure.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"unsupported git archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError(f"could not extract git archive member: {member.name}")
            target.write_bytes(extracted.read())
            os.chmod(target, member.mode & 0o777)


def browser_target_outcome(
    planned_checks: list[dict[str, Any]], evidence: dict[str, Any]
) -> OracleOutcome:
    planned_actions = [
        action
        for check in planned_checks
        for action in (check.get("actions") or [])
        if isinstance(action, dict)
    ]
    if not planned_checks or any(not (check.get("actions") or []) for check in planned_checks):
        return OracleOutcome(
            status="infrastructure_error", target_passed=False,
            preservation_passed=False,
            evidence={"reason": "target_contract_is_missing_actions"},
        )
    if not any(action.get("action") in {"evaluate", "assert_form_valid"} for action in planned_actions):
        return OracleOutcome(
            status="infrastructure_error", target_passed=False,
            preservation_passed=False,
            evidence={"reason": "target_contract_has_no_assertion"},
        )
    observed = evidence.get("checks") or []
    if any(item.get("status") in {"invalid_test_contract", "no_action_contract"} for item in observed):
        return OracleOutcome(
            status="infrastructure_error", target_passed=False,
            preservation_passed=False,
            evidence={"reason": "target_contract_is_not_executable", "checks": observed},
        )
    return OracleOutcome(
        status="ok",
        target_passed=(len(observed) == len(planned_checks) and all(
            item.get("status") == "ok" for item in observed
        )),
        preservation_passed=True,
        evidence={"browser_checks": observed},
    )


class _RealBrowserPatchOracle:
    def __init__(
        self, *, run_dir: Path, frontend: Path, config: HarnessConfig,
        source_commit: str, source_code: dict[str, str], patches: list[AtomicPatch],
        checks: list[dict[str, Any]], baseline: dict[str, Any] | None,
        scope: dict[str, Any] | None, attempt_dir: Path,
    ) -> None:
        self.run_dir = run_dir
        self.frontend = frontend
        self.config = config
        self.source_commit = source_commit
        self.source_code = source_code
        self.patches = patches
        self.checks = checks
        self.baseline = baseline
        self.scope = scope
        self.attempt_dir = attempt_dir
        self.persisted: dict[tuple[str, ...], OracleOutcome] = {}
        existing_attempts = sorted(attempt_dir.glob("attempt_*.json")) if attempt_dir.exists() else []
        self.attempt = len(existing_attempts)
        for path in existing_attempts:
            payload = _read_json(path, {})
            kept = payload.get("kept_change_ids")
            status = payload.get("status")
            if not isinstance(kept, list) or status not in {
                "ok", "candidate_failed", "infrastructure_error"
            }:
                continue
            self.persisted[tuple(str(item) for item in kept)] = OracleOutcome(
                status=status,
                target_passed=payload.get("target_passed") is True,
                preservation_passed=payload.get("preservation_passed") is True,
                evidence=payload.get("evidence") or {},
            )

    async def __call__(self, kept: tuple[str, ...]) -> OracleOutcome:
        if kept in self.persisted:
            return self.persisted[kept]
        self.attempt += 1
        self.attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_path = self.attempt_dir / f"attempt_{self.attempt:03d}.json"
        try:
            candidate = apply_atomic_patches(
                self.source_code, self.patches, set(kept)
            )
        except ValueError as exc:
            outcome = OracleOutcome(
                status="candidate_failed", target_passed=False,
                preservation_passed=False,
                evidence={"reason": "patch_replay_failed", "error": str(exc)},
            )
            _write_json(attempt_path, {"kept_change_ids": list(kept), **outcome.evidence})
            return outcome

        temp_parent = self.attempt_dir / "workspaces"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="candidate_", dir=temp_parent) as raw:
            candidate_run = Path(raw)
            candidate_frontend = candidate_run / "frontend"
            candidate_frontend.mkdir()
            _safe_extract_git_archive(self.frontend, self.source_commit, candidate_frontend)
            for path, content in candidate.items():
                target = candidate_frontend / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            node_modules = self.frontend / "node_modules"
            if node_modules.is_dir() and not (candidate_frontend / "node_modules").exists():
                (candidate_frontend / "node_modules").symlink_to(node_modules, target_is_directory=True)
            seed_manifest = self.run_dir / "seed_manifest.json"
            if seed_manifest.is_file():
                shutil.copy2(seed_manifest, candidate_run / "seed_manifest.json")
            candidate_harness = candidate_run / ".harness"
            candidate_harness.mkdir()
            evidence_path = self.attempt_dir / f"browser_{self.attempt:03d}.json"
            stack = None
            try:
                stack = await start_app_stack(
                    candidate_run, candidate_harness, self.config,
                    round_num=self.attempt,
                )
                evidence = await collect_browser_evidence(
                    app_url=stack.frontend_url, checks=self.checks,
                    output_path=evidence_path, headless=True,
                    fail_fast=True, action_timeout_ms=2_000,
                )
                target_outcome = browser_target_outcome(self.checks, evidence)
                if target_outcome.status == "infrastructure_error":
                    outcome = target_outcome
                else:
                    preservation = True
                    guard: dict[str, Any] | None = None
                    if self.baseline is not None:
                        current = await snapshot_semantic_dom(
                            stack.frontend_url, headless=True
                        )
                        guard = compare_contract(self.baseline, current, self.scope)
                        preservation = guard.get("passed") is True
                    outcome = OracleOutcome(
                        status="ok",
                        target_passed=target_outcome.target_passed,
                        preservation_passed=preservation,
                        evidence={
                            **target_outcome.evidence,
                            "edit_guard": guard,
                            "attempt_file": str(evidence_path),
                        },
                    )
            except Exception as exc:  # candidate subsets may be intentionally unrunnable
                text = f"{type(exc).__name__}: {exc}"
                candidate_failure = any(marker in text.lower() for marker in (
                    "process exited before", "frontend process exited", "syntaxerror",
                    "module not found", "failed to resolve import",
                ))
                outcome = OracleOutcome(
                    status="candidate_failed" if candidate_failure else "infrastructure_error",
                    target_passed=False,
                    preservation_passed=False,
                    evidence={"reason": "candidate_runtime_failed", "error": text},
                )
            finally:
                if stack is not None:
                    await stack.close()

        _write_json(attempt_path, {
            "status": outcome.status,
            "kept_change_ids": list(kept),
            "target_passed": outcome.target_passed,
            "preservation_passed": outcome.preservation_passed,
            "evidence": outcome.evidence,
        })
        return outcome


async def certify_commit_pair(
    *, run_dir: Path, config: HarnessConfig, round_num: int, kind: str,
    source_commit: str, destination_commit: str, checks: list[dict[str, Any]],
    baseline: dict[str, Any] | None, scope: dict[str, Any] | None,
    max_atoms: int,
) -> dict[str, Any]:
    frontend = run_dir / "frontend"
    source_code = _code_at_commit(frontend, source_commit)
    destination_code = _code_at_commit(frontend, destination_commit)
    changed = _changed_paths(frontend, source_commit, destination_commit)
    unsupported = [
        path for path in changed
        if Path(path).name not in IGNORED_CHANGED_FILES
        and Path(path).suffix.lower() not in CODE_EXTENSIONS
    ]
    output_path = run_dir / ".harness" / f"minimality_round_{round_num}_{kind}.json"
    existing_certificate = _read_json(output_path, None)
    if isinstance(existing_certificate, dict) and existing_certificate.get("status"):
        return existing_certificate
    if unsupported:
        certificate = {
            "schema_version": "counterfactual-patch-certificate-v1",
            "status": "inconclusive",
            "reason": "unsupported_changed_files",
            "unsupported_changed_files": unsupported,
        }
        _write_json(output_path, certificate)
        return certificate
    try:
        patches = build_atomic_patches(source_code, destination_code)
    except ValueError as exc:
        certificate = {
            "schema_version": "counterfactual-patch-certificate-v1",
            "status": "inconclusive", "reason": "patch_decomposition_failed",
            "error": str(exc),
        }
        _write_json(output_path, certificate)
        return certificate
    attempt_dir = run_dir / ".harness" / "minimality" / f"round_{round_num}_{kind}"
    oracle = _RealBrowserPatchOracle(
        run_dir=run_dir, frontend=frontend, config=config,
        source_commit=source_commit, source_code=source_code, patches=patches,
        checks=checks, baseline=baseline, scope=scope, attempt_dir=attempt_dir,
    )
    certificate = await asyncio.wait_for(
        certify_patch_minimality(patches, oracle, max_atoms=max_atoms),
        timeout=config.minimality_oracle_timeout_seconds,
    )
    certificate.update({
        "kind": kind,
        "round": round_num,
        "source_commit": source_commit,
        "destination_commit": destination_commit,
        "atomic_patches": [patch.payload() for patch in patches],
    })
    _write_json(output_path, certificate)
    return certificate


async def certify_round_minimality(
    *, run_dir: Path, config: HarnessConfig, round_num: int, sprint_num: int,
    checks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    policy = _read_json(run_dir / ".harness" / POLICY_NAME, None)
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return None
    build_map = _read_json(run_dir / ".harness" / BUILD_MAP_NAME, {})
    current = build_map.get(str(round_num))
    if not isinstance(current, dict) or not current.get("destination_commit"):
        return {
            "status": "inconclusive", "reason": "missing_round_build_mapping"
        }
    records = [
        record for record in build_map.values()
        if isinstance(record, dict) and int(record.get("sprint") or 0) == sprint_num
    ]
    records.sort(key=lambda item: int(item.get("round") or 0))
    repair_baseline = run_dir / ".harness" / repair_baseline_name(round_num)
    baseline = _read_json(
        repair_baseline
        if repair_baseline.is_file()
        else run_dir / ".harness" / sprint_baseline_name(sprint_num),
        None,
    )
    scope = _read_json(
        run_dir / ".harness" / f"edit_scope_round_{round_num}.json", None
    )
    max_atoms = int(policy.get("max_atomic_changes") or config.minimality_max_atoms)
    result: dict[str, Any] = {"status": "ok", "certificates": {}}
    if (run_dir / "seed_manifest.json").is_file():
        edit_source = str(records[0]["source_commit"])
        result["certificates"]["edit"] = await certify_commit_pair(
            run_dir=run_dir, config=config, round_num=round_num, kind="edit",
            source_commit=edit_source,
            destination_commit=str(current["destination_commit"]),
            checks=checks, baseline=baseline, scope=scope, max_atoms=max_atoms,
        )
    if current.get("mode") == "repair":
        result["certificates"]["repair"] = await certify_commit_pair(
            run_dir=run_dir, config=config, round_num=round_num, kind="repair",
            source_commit=str(current["source_commit"]),
            destination_commit=str(current["destination_commit"]),
            checks=checks, baseline=baseline, scope=scope, max_atoms=max_atoms,
        )
    return result


__all__ = [
    "browser_target_outcome",
    "certify_commit_pair",
    "certify_round_minimality",
    "ensure_minimality_policy",
    "record_round_build_destination",
    "record_round_build_source",
]
