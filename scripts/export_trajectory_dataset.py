"""Export a completed Harness trajectory in the construct/ records.jsonl schema."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.task_inputs import task_input_image_paths
from src.orchestration.accepted_tapes import select_accepted_replay_checks
from src.orchestration.edit_card import read_edit_card
from src.orchestration.minimal_path_guidance import SUPPORTED_PLAN_VERSIONS
from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS
from src.orchestration.webcompass_protocol import REPAIR_TYPES


CODE_EXTENSIONS = {
    ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
    ".vue", ".svelte", ".svg", ".json", ".json5", ".qml", ".ets",
    ".wxml", ".wxss",
}
IGNORED_CODE_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
ATOMIC_EDIT_TASK_COUNT = 1
COMPOUND_EDIT_MIN_TASKS = 4
COMPOUND_EDIT_MAX_TASKS = 12
INFRA_FAILURE_MARKERS = (
    "vision scorer unavailable",
    "request timed out",
    "rate limit",
    "provider error",
    "harness error",
    "playwright error",
    "timed out waiting for frontend",
    "http error 502",
    "connection refused",
)
UNCERTAIN_FAILURE_MARKERS = (
    "could not verify",
    "could not be fully verified",
    "not verified",
    "unable to verify",
    "within evaluation budget",
    "likely",
    "may be",
    "appears to",
    "cannot confirm",
    "not confirmed",
    "not observed",
    "not directly verified",
    "may not",
    "appear incomplete",
    "not captured",
    "budget exhaustion",
    "not tested",
    "not fully tested",
    "not fully verified",
    "testing constraints",
)


def _git(frontend: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=frontend, text=True, capture_output=True, check=True
    )
    return result.stdout


def agent_commits(frontend: Path) -> list[str]:
    commits = _git(frontend, "rev-list", "--reverse", "HEAD").splitlines()
    selected = []
    for commit in commits:
        subject = _git(frontend, "show", "-s", "--format=%s", commit).strip().lower()
        if subject.startswith(("feat", "fix")):
            selected.append(commit)
    return selected


def code_at_commit(frontend: Path, commit: str) -> list[dict[str, str]]:
    paths = _git(frontend, "ls-tree", "-r", "--name-only", commit).splitlines()
    bundle = []
    for path in sorted(paths):
        if (
            Path(path).suffix.lower() not in CODE_EXTENSIONS
            or Path(path).name in IGNORED_CODE_FILES
        ):
            continue
        code = _git(frontend, "show", f"{commit}:{path}")
        bundle.append({"path": path, "code": code})
    return bundle


def make_patches(
    src_code: list[dict[str, str]],
    dst_code: list[dict[str, str]],
    task_type: str,
) -> list[dict[str, str]]:
    src = {item["path"]: item["code"] for item in src_code}
    dst = {item["path"]: item["code"] for item in dst_code}
    deleted = sorted(set(src) - set(dst))
    if deleted:
        raise ValueError(f"construct patch schema cannot delete files: {deleted}")
    patches = []
    for path in sorted(dst):
        before = src.get(path)
        after = dst[path]
        if before == after:
            continue
        if before is None:
            patches.append(
                {
                    "path": path,
                    "operation": "create_file",
                    "content": after,
                    "task_type": task_type,
                }
            )
            continue
        try:
            localized = _localized_changes(before, after)
        except ValueError as exc:
            raise ValueError(f"cannot export local exact patch for {path}: {exc}") from exc
        patches.extend(
            {
                "path": path,
                "search": search,
                "replace": replace,
                "task_type": task_type,
            }
            for search, replace in localized
        )
    return patches


def _localized_changes(
    before: str, after: str, context_lines: int = 1
) -> list[tuple[str, str]]:
    """Return replayable, unique local hunks or fail closed.

    A whole-file Search/Replace makes an otherwise small Edit look like a full
    rewrite and destroys the unchanged-region signal.  Grow local context only
    far enough to make every search unique.  A bounded character fallback
    supports minified or single-line sources without permitting a full-file
    replacement.
    """
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    def candidate(
        old_units: Any,
        new_units: Any,
        context: int,
    ) -> list[tuple[str, str]] | None:
        # get_grouped_opcodes() trims the cached boundary opcodes in place, so
        # each context attempt needs a fresh matcher.
        sequence_matcher = difflib.SequenceMatcher(
            a=old_units, b=new_units, autojunk=False
        )
        changes: list[tuple[str, str]] = []
        current = before
        for group in sequence_matcher.get_grouped_opcodes(n=context):
            old_start, old_end = group[0][1], group[-1][2]
            new_start, new_end = group[0][3], group[-1][4]
            search = "".join(old_units[old_start:old_end])
            replace = "".join(new_units[new_start:new_end])
            if (
                not search
                or search == before
                or before.count(search) != 1
                or current.count(search) != 1
            ):
                return None
            current = current.replace(search, replace, 1)
            changes.append((search, replace))
        if changes and current == after:
            return changes
        return None

    max_line_context = min(max(len(old), len(new)), 64)
    for context in range(max(1, context_lines), max_line_context + 1):
        changes = candidate(old, new, context)
        if changes is not None:
            return changes

    # Line-granularity cannot localize edits inside a single long/minified line.
    # Keep this deliberately bounded: beyond this point the record should be
    # rejected and regenerated instead of disguising a broad rewrite as a patch.
    max_char_context = min(max(len(before), len(after)), 256)
    for context in range(1, max_char_context + 1):
        changes = candidate(before, after, context)
        if changes is not None:
            return changes

    raise ValueError(
        "no unique bounded local Search/Replace exists; regenerate a narrower edit"
    )


def apply_patches(
    src_code: list[dict[str, str]], patches: list[dict[str, str]]
) -> list[dict[str, str]]:
    code = {item["path"]: item["code"] for item in src_code}
    for patch in patches:
        path = patch["path"]
        operation = patch.get("operation", "replace")
        if operation == "create_file":
            if path in code or not isinstance(patch.get("content"), str):
                raise ValueError(f"invalid create_file patch: {path}")
            code[path] = patch["content"]
            continue
        if operation != "replace":
            raise ValueError(f"unsupported patch operation {operation!r}: {path}")
        current = code.get(path, "")
        search = patch.get("search")
        if not isinstance(search, str):
            raise ValueError(f"replace patch has no exact search: {path}")
        if search == "" and path not in code:
            code[path] = patch["replace"]
        elif current.count(search) == 1:
            code[path] = current.replace(search, patch["replace"], 1)
        elif search in current:
            raise ValueError(f"patch search is not unique: {path}")
        else:
            raise ValueError(f"patch search not found: {path}")
    return [{"path": path, "code": code[path]} for path in sorted(code)]


def _patch_stats(patches: list[dict[str, str]]) -> dict[str, Any]:
    added = sum(
        len(
            str(
                patch.get("content")
                if patch.get("operation") == "create_file"
                else patch.get("replace", "")
            ).splitlines()
        )
        for patch in patches
    )
    removed = sum(
        0
        if patch.get("operation") == "create_file"
        else len(str(patch.get("search", "")).splitlines())
        for patch in patches
    )
    return {
        "changed_file_count": len({patch["path"] for patch in patches}),
        "added_lines_in_patch_regions": added,
        "removed_lines_in_patch_regions": removed,
        "total_patch_lines": added + removed,
    }


def _edit_kind(task_types: list[str]) -> str | None:
    task_count = len(task_types)
    if task_count == ATOMIC_EDIT_TASK_COUNT:
        return "atomic_edit"
    if COMPOUND_EDIT_MIN_TASKS <= task_count <= COMPOUND_EDIT_MAX_TASKS:
        return "compound_edit"
    return None


def _quality_tier(task: str, patches: list[dict[str, str]], task_types: list[str]) -> tuple[str, list[str]]:
    stats = _patch_stats(patches)
    reasons: list[str] = []
    if any(patch.get("operation") == "create_file" for patch in patches):
        reasons.append("explicit_file_creation_requires_native_schema")
    if any(
        patch.get("operation", "replace") == "replace" and not patch.get("search")
        for patch in patches
    ):
        reasons.append("empty_search_not_reverse_compatible")
    if task == "text-editing":
        if _edit_kind(task_types) is None:
            reasons.append("edit_task_count_not_1_or_4_to_12")
        if stats["changed_file_count"] > 8:
            reasons.append("edit_changes_too_many_files")
        if stats["total_patch_lines"] > 2500:
            reasons.append("edit_diff_too_large")
    elif task == "text-repair":
        if stats["changed_file_count"] > 4:
            reasons.append("repair_changes_too_many_files")
        if stats["total_patch_lines"] > 1000:
            reasons.append("repair_diff_too_large")
    return ("benchmark_aligned" if not reasons else "natural_trajectory", reasons)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _round_files(harness: Path, prefix: str) -> dict[int, dict[str, Any]]:
    result = {}
    for path in harness.glob(f"{prefix}_round_*.json"):
        try:
            round_num = int(path.stem.rsplit("_", 1)[-1])
            result[round_num] = json.loads(path.read_text())
        except (ValueError, json.JSONDecodeError):
            continue
    return result


def screenshots_for_round(run_dir: Path, round_num: int) -> list[dict[str, str]]:
    manifest = _read_json(
        run_dir / ".harness" / f"visual_manifest_round_{round_num}.json", {}
    )
    records = []
    for relative in manifest.get("screenshots", []):
        path = (run_dir / relative).resolve()
        if path.is_file():
            records.append({"path": str(path), "kind": "harness_reviewed_render"})
    return records


def _sprint_map(harness: Path) -> dict[int, dict[str, Any]]:
    plan = _read_json(harness / "sprint_plan.json", {"sprints": []})
    return {int(item["number"]): item for item in plan.get("sprints", [])}


def _task_types(harness: Path, sprint_num: int) -> list[str]:
    features = _read_json(harness / "feature_list.json", {"features": []})
    names = [
        str(item.get("name") or item.get("id"))
        for item in features.get("features", [])
        if int(item.get("sprint") or 0) == sprint_num
    ]
    return names or [f"Sprint {sprint_num}"]


def _task_descriptions(harness: Path, sprint_num: int) -> list[dict[str, str]]:
    """Preserve the planner's per-feature request in reverse-v2 edit form."""
    features = _read_json(harness / "feature_list.json", {"features": []})
    descriptions = []
    for item in features.get("features", []):
        if int(item.get("sprint") or 0) != sprint_num:
            continue
        task_type = str(item.get("name") or item.get("id") or "")
        description = str(item.get("description") or item.get("acceptance_criteria") or "").strip()
        if task_type and description:
            descriptions.append({"task_type": task_type, "description": description})
    return descriptions


def _sprint_description(sprint: dict[str, Any]) -> str:
    lines = [str(sprint.get("title", "")), str(sprint.get("goal", ""))]
    lines.extend(str(item) for item in sprint.get("deliverables", []))
    return "\n".join(line for line in lines if line).strip()


def _cumulative_description(sprints: dict[int, dict[str, Any]], target: int) -> str:
    return "\n\n".join(
        f"Sprint {number}\n{_sprint_description(sprints[number])}"
        for number in sorted(sprints)
        if number <= target
    )


def _generation_description(
    harness: Path, sprints: dict[int, dict[str, Any]], target: int
) -> str:
    """Return the complete parent requirement, not a synthetic per-sprint query."""
    spec_path = harness / "spec.md"
    if spec_path.is_file():
        spec = spec_path.read_text(encoding="utf-8").strip()
        if spec:
            return spec
    state = _read_json(harness / "harness_state.json", {})
    prompt = str(state.get("prompt") or "").strip()
    return prompt or _cumulative_description(sprints, target)


def _is_real_project_failure(grade: dict[str, Any]) -> bool:
    # A scope audit by itself is provenance metadata, not a user-visible bug.
    # It must not, however, erase a separately reproduced UI failure in the
    # same round; the subsequent repair can legitimately fix that UI defect.
    if grade.get("overall_passed") is not False:
        return False
    if grade.get("edit_scope_audit") == "fail":
        reproduced_critical = any(
            isinstance(item, dict) and item.get("critical") is True
            and str(item.get("status", "")).lower() == "fail"
            for item in grade.get("ui_checks") or []
        ) or any(
            isinstance(item, dict) and item.get("critical") is True
            and item.get("passed") is False
            for item in grade.get("target_exit_criteria_results") or []
        ) or bool((grade.get("hidden_oracle") or {}).get("failed_check_ids"))
        if not reproduced_critical:
            return False
    text = json.dumps(grade, ensure_ascii=False).lower()
    return (
        not any(marker in text for marker in INFRA_FAILURE_MARKERS)
        and bool(_confirmed_failure_evidence(grade))
    )


def _is_confirmed_failure_text(value: Any) -> bool:
    text = str(value or "").strip()
    lowered = text.lower()
    return bool(text) and not any(marker in lowered for marker in UNCERTAIN_FAILURE_MARKERS)


def _has_complete_critical_coverage(grade: dict[str, Any]) -> bool:
    """Require observed evidence before accepting a forward edit as training data.

    The harness may accept an evaluator-coverage gap so it does not invent a
    repair task from an unobserved defect.  That is appropriate for runtime
    control flow, but an edit example with an unexercised critical interaction
    is not a trustworthy positive training pair and must stay out of exports.
    """
    for check in grade.get("ui_checks") or []:
        if not isinstance(check, dict) or not check.get("critical"):
            continue
        if str(check.get("status", "")).strip().lower() != "pass":
            return False
        if not _is_confirmed_failure_text(check.get("notes")):
            return False
    for result in grade.get("target_exit_criteria_results") or []:
        if not isinstance(result, dict) or not result.get("critical"):
            continue
        if result.get("passed") is not True:
            return False
        if not _is_confirmed_failure_text(result.get("notes")):
            return False
    return True


def _trace_has_no_failed_browser_click(harness: Path, round_num: int) -> bool:
    """Require a real user-level click, not merely a forced trace action."""
    path = harness / "traces" / f"evaluator_round_{round_num}.jsonl"
    if not path.is_file():
        return True
    forced_clicks: list[str] = []
    normal_successes: set[str] = set()
    pending_clicks: list[tuple[str, bool]] = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "assistant":
            for tool_call in (event.get("message") or {}).get("tool_calls") or []:
                function = tool_call.get("function") or {}
                if function.get("name") != "browser_click":
                    continue
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                pending_clicks.append(
                    (str(arguments.get("selector") or "<unknown>"), bool(arguments.get("force")))
                )
            continue
        if event.get("event") != "tool" or event.get("name") != "browser_click":
            continue
        selector, forced = pending_clicks.pop(0) if pending_clicks else ("<unknown>", False)
        if event.get("ok") is False:
            return False
        if forced:
            forced_clicks.append(selector)
        else:
            normal_successes.add(selector)
    return all(selector in normal_successes for selector in forced_clicks)


def _minimality_certificate(
    harness: Path, round_num: int, kind: str
) -> dict[str, Any] | None:
    """Return a certificate only when the run opted into the new hard gate."""
    policy = _read_json(harness / "minimality_policy.json", {})
    if policy.get("enabled") is not True:
        return None
    certificate = _read_json(
        harness / f"minimality_round_{round_num}_{kind}.json", {}
    )
    return certificate if isinstance(certificate, dict) else {}


def _minimality_export_passed(harness: Path, round_num: int, kind: str) -> bool:
    certificate = _minimality_certificate(harness, round_num, kind)
    return certificate is not None and certificate.get("status") == "certified"


def _minimality_pair_matches(
    harness: Path,
    round_num: int,
    kind: str,
    *,
    source_commit: str,
    destination_commit: str,
    failure_round: int | None = None,
) -> bool:
    certificate = _minimality_certificate(harness, round_num, kind) or {}
    policy = _read_json(harness / "minimality_policy.json", {})
    strict_pair_provenance = policy.get("schema_version") == "minimality-policy-v1"
    if not strict_pair_provenance and not certificate.get("source_commit"):
        return certificate.get("status") == "certified"
    if (
        certificate.get("status") != "certified"
        or certificate.get("source_commit") != source_commit
        or certificate.get("destination_commit") != destination_commit
    ):
        return False
    return failure_round is None or certificate.get("failure_round") == failure_round


def _accepted_tape_records(harness: Path) -> list[dict[str, Any]]:
    path = harness / "accepted_tapes.jsonl"
    if not path.is_file():
        return []
    accepted: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return []
        checks = record.get("checks") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != "accepted-tape-v1"
            or record.get("status") != "ok"
            or not isinstance(record.get("sprint"), int)
            or not isinstance(record.get("round"), int)
            or not isinstance(checks, list)
            or not checks
        ):
            return []
        for check in checks:
            actions = check.get("actions") if isinstance(check, dict) else None
            assertion_count = (
                sum(
                    isinstance(action, dict)
                    and action.get("action") in TYPED_ASSERTION_ACTIONS
                    for action in actions
                )
                if isinstance(actions, list)
                else 0
            )
            if (
                not isinstance(actions, list)
                or not actions
                or assertion_count < 1
                or not isinstance(actions[-1], dict)
                or actions[-1].get("action") not in TYPED_ASSERTION_ACTIONS
            ):
                return []
        evidence_ref = str(record.get("evidence_ref") or "")
        if (
            record.get("round") == 0
            and record.get("lineage_source") == "sequential_edit_chain"
            and evidence_ref == "chain_parent_accepted_checkpoint"
        ):
            # Transferred obligations are not local accepted checkpoints. Keep
            # them for the final-state replay gate, which verifies their IDs
            # and actual results in this workdir.
            accepted.append(record)
            continue
        if not evidence_ref.startswith(".harness/browser_evidence_round_"):
            return []
        evidence = _read_json(harness.parent / evidence_ref, {})
        observed = evidence.get("checks") if isinstance(evidence, dict) else None
        if not isinstance(observed, list) or len(observed) != len(checks) or any(
            not isinstance(item, dict) or item.get("status") != "ok"
            for item in observed
        ):
            return []
        accepted.append(record)
    return accepted


def _accepted_tape_rounds(harness: Path) -> set[int]:
    return {int(record["round"]) for record in _accepted_tape_records(harness) if record["round"] > 0}


def _accepted_tape_replay_passed(
    harness: Path, *, round_num: int, sprint_num: int
) -> bool:
    """Require a fresh final-state replay of every earlier accepted sprint."""
    records = _accepted_tape_records(harness)
    prior_checks = [
        check
        for record in sorted(records, key=lambda item: int(item["sprint"]))
        if int(record["round"]) < round_num
        for check in record["checks"]
    ]
    selection_path = harness / f"regression_selection_round_{round_num}.json"
    if selection_path.is_file():
        selection = _read_json(selection_path, {})
        card = read_edit_card(harness)
        if card is None or selection.get("schema_version") != "regression-selection-v1":
            return False
        expected = select_accepted_replay_checks(
            harness,
            edit_card=card,
            accepted_edit_index=int(selection.get("accepted_edit_index") or 0),
            full_replay_interval=int(selection.get("full_replay_interval") or 5),
            before_round=round_num,
        )
        expected_checks = expected.pop("checks")
        if any(selection.get(key) != expected.get(key) for key in expected):
            return False
        prior_checks = expected_checks
    if not prior_checks:
        return True
    evidence = _read_json(
        harness / f"accepted_tape_replay_round_{round_num}.json", {}
    )
    observed = evidence.get("checks") if isinstance(evidence, dict) else None
    if not isinstance(observed, list) or len(observed) != len(prior_checks):
        return False
    expected_ids = [str(check.get("id")) for check in prior_checks]
    observed_ids = [str(item.get("check_id")) for item in observed if isinstance(item, dict)]
    return observed_ids == expected_ids and all(
        isinstance(item, dict) and item.get("status") == "ok" for item in observed
    )


def _strict_mutation_evidence_passed(
    harness: Path, round_num: int, kind: str, *, require_minimality: bool = True
) -> bool:
    if not require_minimality and _read_json(harness / "harness_state.json", {}).get("supplied_atomic_plan"):
        build = _read_json(harness / "round_build_map.json", {}).get(str(round_num), {})
        return bool(build.get("source_commit") and build.get("destination_commit")
                    and build["source_commit"] != build["destination_commit"])
    if require_minimality and not _minimality_export_passed(harness, round_num, kind):
        return False

    if not _minimal_path_artifacts_ready(harness, round_num):
        return False
    return _linked_mutation_round(harness, round_num) is not None


def _minimal_path_artifacts_ready(harness: Path, round_num: int) -> bool:
    """Validate the harness-owned plan, semantic scope, and stable baseline."""
    plan = _read_json(harness / f"minimal_path_plan_round_{round_num}.json", {})
    scope = _read_json(harness / f"edit_scope_round_{round_num}.json", {})
    if (
        plan.get("schema_version") not in SUPPORTED_PLAN_VERSIONS - {"minimal-path-plan-v1", "minimal-path-plan-v2"}
        or plan.get("owner") != "harness"
        or plan.get("status") != "ready"
        or scope.get("schema_version") != "edit-scope-v4"
        or scope.get("owner") != "harness"
        or not isinstance(scope.get("allowed_fragment_keys"), list)
        or not isinstance(scope.get("expected_new_fragments"), list)
    ):
        return False
    baseline_ref = scope.get("baseline")
    if not isinstance(baseline_ref, str) or not baseline_ref.startswith(".harness/"):
        return False
    baseline = _read_json(harness.parent / baseline_ref, {})
    return baseline.get("version") == 4 and baseline.get("stable") is True


def _minimal_path_decisions(harness: Path, round_num: int) -> list[str]:
    ledger_path = harness / f"minimal_path_ledger_round_{round_num}.jsonl"
    if not ledger_path.is_file():
        return []
    decisions: list[str] = []
    for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return []
        if isinstance(item, dict):
            decisions.append(str(item.get("decision", "")))
    return decisions


def _linked_mutation_round(harness: Path, round_num: int) -> int | None:
    """Resolve the actual mutation behind a possibly zero-mutation evidence round."""
    decisions = _minimal_path_decisions(harness, round_num)
    if "applied" in decisions and "validation_pass" in decisions:
        return round_num

    build_map = _read_json(harness / "round_build_map.json", {})
    current = build_map.get(str(round_num)) if isinstance(build_map, dict) else None
    if (
        not isinstance(current, dict)
        or not current.get("source_commit")
        or current.get("source_commit") != current.get("destination_commit")
    ):
        return None
    destination = str(current["destination_commit"])
    sprint = current.get("sprint")
    candidates: list[int] = []
    for raw_round, build in build_map.items():
        try:
            candidate = int(raw_round)
        except (TypeError, ValueError):
            continue
        if (
            candidate >= round_num
            or not isinstance(build, dict)
            or build.get("sprint") != sprint
            or str(build.get("destination_commit") or "") != destination
            or build.get("source_commit") == build.get("destination_commit")
            or not _minimal_path_artifacts_ready(harness, candidate)
        ):
            continue
        candidate_decisions = _minimal_path_decisions(harness, candidate)
        if "applied" in candidate_decisions and "validation_pass" in candidate_decisions:
            candidates.append(candidate)
    return max(candidates, default=None)


def _failure_has_runtime_evidence(
    harness: Path, round_num: int, grade: dict[str, Any]
) -> bool:
    browser = _read_json(harness / f"browser_evidence_round_{round_num}.json", {})
    if any(
        isinstance(item, dict) and item.get("status") == "action_failed"
        for item in browser.get("checks", [])
    ):
        return True
    tape = _read_json(harness / f"accepted_tape_replay_round_{round_num}.json", {})
    if any(
        isinstance(item, dict) and item.get("status") == "action_failed"
        for item in tape.get("checks", [])
    ):
        return True
    phase = grade.get("phase_results") or {}
    if phase.get("render_gate") == "fail":
        return True
    return phase.get("appearance") == "fail" and bool(
        screenshots_for_round(harness.parent, round_num)
    )


def _minimal_path_provenance(harness: Path, round_num: int) -> dict[str, Any]:
    """Summarize online guidance separately from post-hoc minimality proof."""
    mutation_round = _linked_mutation_round(harness, round_num) or round_num
    plan_path = harness / f"minimal_path_plan_round_{mutation_round}.json"
    plan = _read_json(plan_path, {})
    if not isinstance(plan, dict) or plan.get("owner") != "harness":
        return {"status": "legacy_not_required"}
    ledger_path = harness / f"minimal_path_ledger_round_{mutation_round}.jsonl"
    ledger: list[dict[str, Any]] = []
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                ledger.append(item)
    counts = {
        decision: sum(item.get("decision") == decision for item in ledger)
        for decision in ("allow", "deny")
    }
    transitions = {
        decision: sum(item.get("decision") == decision for item in ledger)
        for decision in (
            "observe",
            "applied",
            "failed",
            "validation_pass",
            "validation_fail",
        )
    }
    state_path = harness / f"minimal_path_state_round_{mutation_round}.json"
    state = _read_json(state_path, {})
    has_applied_outcomes = any(
        item.get("decision") == "applied" for item in ledger
    )
    touched_decision = "applied" if has_applied_outcomes else "allow"
    cone = plan.get("source_change_cone") or {}
    dom = plan.get("dom_change_cone") or {}
    route_scope = plan.get("route_scope") or {}
    return {
        "status": "enforced",
        "mutation_round": mutation_round,
        "evidence_round": round_num,
        "plan_artifact": f".harness/{plan_path.name}",
        "ledger_artifact": f".harness/{ledger_path.name}",
        "state_artifact": (
            f".harness/{state_path.name}" if state_path.is_file() else None
        ),
        "initial_paths": list(cone.get("initial_paths") or []),
        "local_paths": list(cone.get("local_paths") or []),
        "dependency_paths": list(cone.get("dependency_paths") or []),
        "allowed_root_keys": list(dom.get("allowed_root_keys") or []),
        "target_routes": list(route_scope.get("target_routes") or []),
        "protected_routes": list(route_scope.get("protected_routes") or []),
        "cross_route_shared_paths": list(
            route_scope.get("cross_route_shared_paths") or []
        ),
        "off_target_paths": list(route_scope.get("off_target_paths") or []),
        "decision_counts": counts,
        "transition_counts": transitions,
        "controller_phase": state.get("phase"),
        "validation_attempt_revision": state.get("validation_attempt_revision"),
        "validation_success_revision": state.get("validation_success_revision"),
        "validation_last_ok": state.get("validation_last_ok"),
        "touched_paths": sorted({
            str(item["path"])
            for item in ledger
            if item.get("decision") == touched_decision and item.get("path")
        }),
        "dependency_expansions": sorted({
            str(item["path"])
            for item in ledger
            if item.get("expansion_reason") == "recorded_dependency_edge"
            and item.get("path")
        }),
    }


def _confirmed_failure_evidence(grade: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for item in grade.get("target_exit_criteria_results") or []:
        if item.get("passed") is False and _is_confirmed_failure_text(item.get("notes")):
            evidence.append(str(item["notes"]).strip())
    for item in grade.get("ui_checks") or []:
        if str(item.get("status", "")).lower() == "fail" and _is_confirmed_failure_text(item.get("notes")):
            evidence.append(str(item["notes"]).strip())
    phase = grade.get("phase_results") or {}
    if phase.get("render_gate") == "fail":
        for bug in grade.get("bugs_found") or []:
            if _is_confirmed_failure_text(bug):
                evidence.append(str(bug).strip())
    if phase.get("appearance") == "fail":
        criteria = grade.get("criteria") or {}
        for name in ("design_quality", "originality", "craft"):
            item = criteria.get(name) or {}
            if item.get("passed") is False and _is_confirmed_failure_text(item.get("notes")):
                evidence.append(f"{name}: {str(item['notes']).strip()}")
    failed_hidden_ids = {
        str(value)
        for value in (grade.get("hidden_oracle") or {}).get("failed_check_ids") or []
        if str(value)
    }
    for item in grade.get("repair_task_descriptions") or []:
        if not isinstance(item, dict) or item.get("task_type") not in REPAIR_TYPES:
            continue
        evidence_ids = {str(value) for value in item.get("evidence_ids") or []}
        description = str(item.get("description") or "").strip()
        if evidence_ids & failed_hidden_ids and _is_confirmed_failure_text(description):
            evidence.append(description)
    return list(dict.fromkeys(evidence))


def _repair_description(grade: dict[str, Any]) -> str:
    evidence = _confirmed_failure_evidence(grade)
    instructions = [
        str(item).strip() for item in grade.get("repair_instructions", [])
        if _is_confirmed_failure_text(item)
    ]
    lines = ["Repair the following reproduced project defects:"]
    lines.extend(f"- Evidence: {item}" for item in evidence)
    lines.extend(f"- Required fix: {item}" for item in instructions)
    return "\n".join(lines)


def _repair_task_descriptions(
    grade: dict[str, Any], *, hidden_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate evidence-grounded, post-hoc labels for observed failures."""
    allowed_evidence_ids = {
        str(item.get("check_id"))
        for item in grade.get("ui_checks") or []
        if isinstance(item, dict)
        and str(item.get("status", "")).lower() == "fail"
        and item.get("check_id")
    } | {
        str(item.get("criterion_id"))
        for item in grade.get("target_exit_criteria_results") or []
        if isinstance(item, dict)
        and item.get("passed") is False
        and item.get("criterion_id")
    }
    functionality = (grade.get("criteria") or {}).get("functionality") or {}
    if isinstance(functionality, dict) and functionality.get("passed") is False:
        allowed_evidence_ids.add("FUNCTIONALITY")
    hidden_oracle = grade.get("hidden_oracle") or {}
    if isinstance(hidden_oracle, dict) and hidden_oracle.get("status") == "failed":
        allowed_evidence_ids.update(
            str(value).strip()
            for value in hidden_oracle.get("failed_check_ids") or []
            if str(value).strip()
        )

    result: list[dict[str, Any]] = []
    used_locations: set[tuple[str, str, str]] = set()
    observed_risks = {str(item.get("check_id")): item for item in (hidden_evidence or {}).get("checks", [])}
    risk_ids = set(hidden_oracle.get("failed_check_ids") or [])
    for item in grade.get("repair_task_descriptions") or []:
        if not isinstance(item, dict):
            continue
        task_type = str(item.get("task_type") or "").strip()
        description = str(item.get("description") or "").strip()
        evidence_ids = [
            str(value).strip()
            for value in item.get("evidence_ids") or []
            if str(value).strip()
        ]
        if (
            task_type not in REPAIR_TYPES
            or not description
            or not evidence_ids
            or any(value not in allowed_evidence_ids for value in evidence_ids)
        ):
            continue
        if hidden_evidence is not None and any(value in risk_ids for value in evidence_ids):
            issues = []
            for evidence_id in evidence_ids:
                for step in observed_risks.get(evidence_id, {}).get("steps", []):
                    output = step.get("output")
                    actual = output.get("actual") if isinstance(output, dict) else None
                    if (step.get("action") != "assert_webcompass_risk" or step.get("ok") is not False
                            or not isinstance(actual, dict) or actual.get("defect_type") != task_type
                            or actual.get("passed") is not False):
                        continue
                    for issue in actual.get("issues") or []:
                        # Apply the corrected table-box detector semantics to historical evidence.
                        if (task_type == "Crowding" and issue.get("kind") == "less-than-2px-gap"
                                and re.search(r"(?:^| > )(?:tr|th|td)(?::nth-of-type\(\d+\))?$",
                                              str(issue.get("element_path") or ""))):
                            continue
                        issues.append(issue)
            locations = item.get("issue_locations") or []
            if locations:
                requested = {(str(loc.get("element_path")), str(loc.get("kind"))) for loc in locations}
                observed = {(str(issue.get("element_path")), str(issue.get("kind"))) for issue in issues}
                if not requested <= observed:
                    continue
                signatures = {(task_type, path, kind) for path, kind in requested}
                if signatures & used_locations:
                    continue
                used_locations.update(signatures)
                issues = [issue for issue in issues if (str(issue.get("element_path")), str(issue.get("kind"))) in requested]
            if not issues:
                continue
            if task_type == "Missing Attributes" and item.get("issue_locations"):
                # Different controls need separate attribute changes even when
                # the model puts their locations in one category-level entry.
                points = {(str(issue.get("element_path")), str(issue.get("kind"))): issue for issue in issues}
                for issue in points.values():
                    result.append({"task_type": task_type,
                        "description": f"{issue['kind']} at {issue.get('element', issue['element_path'])}.",
                        "evidence_ids": list(dict.fromkeys(evidence_ids))})
                continue
            description = task_type + " reproduced after the normal Edit: " + "; ".join(
                f"{issue.get('kind', 'defect')} at {issue.get('element', 'target surface')}" for issue in issues
            ) + "."
        result.append({
            "task_type": task_type,
            "description": description,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        })
    return result


def _base_record(
    *, run_dir: Path, instance_id: str, task: str, task_types: list[str],
    description: str, src_code: list[dict[str, str]], dst_code: list[dict[str, str]],
    patches: list[dict[str, str]], src_images: list[dict[str, str]],
    dst_images: list[dict[str, str]], source_commit: str | None,
    destination_commit: str, quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "source_project": str((run_dir / "frontend").resolve()),
        "task": task,
        "status": "ok",
        "task_type": task_types,
        "description": description,
        "instruction": {
            "src_code": src_code,
            "description": description,
            "source_manifest": {"javascript": [], "stylesheet_bundles": []},
        },
        "reference": {"dst_code": dst_code},
        "label_modified_files": patches,
        "images": {
            "input_images": [
                {"path": str(path), "kind": "user_task_input"}
                for path in task_input_image_paths(run_dir)
            ],
            "src_screenshot": src_images,
            "dst_screenshot": dst_images,
        },
        "llm_response": "",
        "trajectory": {
            "source_commit": source_commit,
            "destination_commit": destination_commit,
            "chain_metadata": _read_json(
                run_dir / ".harness" / "edit_task_contract.json", {}
            ).get("chain_metadata"),
        },
        "quality": quality or {},
    }


def _v2_file_manifest(code: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Emit the portable subset of the reverse-construction v2 manifest."""
    return [
        {"path": item["path"], "type": "code", "size_bytes": len(item["code"].encode("utf-8"))}
        for item in code
    ]


def to_v2_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Convert verified forward records to the reverse-construction v2 contracts.

    Repair deliberately omits the evaluator diagnosis from the training input.
    The diagnosis remains only in metadata for audit, matching the reverse
    repair contract where the model receives broken code but no bug query.
    """
    output: dict[str, list[dict[str, Any]]] = {
        "text-edit.v2": [], "image-edit.v2": [],
        "text-repair.v2": [], "image-repair.v2": [],
    }
    for record in records:
        task = record.get("task")
        if task not in {"text-editing", "text-repair"}:
            continue
        src_code = record["instruction"]["src_code"]
        patches = record["label_modified_files"]
        edit_kind = _edit_kind(record["task_type"]) if task == "text-editing" else None
        declared_edit_kind = (record.get("quality") or {}).get("edit_kind")
        if task == "text-editing" and (
            edit_kind is None
            or declared_edit_kind not in {None, edit_kind}
        ):
            continue
        repair_descriptions = (
            (record.get("quality") or {}).get("repair_task_descriptions") or []
            if task == "text-repair"
            else []
        )
        if task == "text-repair" and (
            not 4 <= len(repair_descriptions) <= 12
            or record["task_type"]
            != [item.get("task_type") for item in repair_descriptions]
            or any(task_type not in REPAIR_TYPES for task_type in record["task_type"])
        ):
            # Native trajectories may contain fewer naturally observed issues.
            # The strict WebCompass-shaped view admits only a naturally
            # occurring 4--12 issue bundle with evidence-grounded labels.
            continue
        # The reverse release requires every patch search to be non-empty and
        # unique. Forward records with file-creation patches remain useful
        # natural trajectories, but must not silently enter the v2-aligned set.
        if any(
            patch.get("operation", "replace") != "replace"
            or not patch.get("search")
            for patch in patches
        ):
            continue
        common = {
            "instance_id": record["instance_id"],
            "status": "ok",
            "task_type": record["task_type"],
            **({"edit_kind": edit_kind} if edit_kind else {}),
            "page_type": "forward_harness",
            "file_manifest": _v2_file_manifest(src_code),
            "resources": [],
        }
        metadata = {
            "release_schema_reference": "webcoding-sft-v2",
            "source_project": record.get("source_project", ""),
            "task_count": len(record["task_type"]),
            **({"edit_kind": edit_kind} if edit_kind else {}),
            "patch_count": len(patches),
            "patch_count_by_task": {
                task_type: sum(patch.get("task_type") == task_type for patch in patches)
                for task_type in record["task_type"]
            },
            "prompt_tokens": sum(len(item.get("code", "")) for item in src_code),
            "input_contract": {"max_prompt_tokens": 40000, "all_files_included": True},
            "construction_model": "qwen3.6-plus",
            "construction_source": "forward_harness",
            "chain_metadata": record["trajectory"].get("chain_metadata"),
            "source_commit": record["trajectory"]["source_commit"],
            "destination_commit": record["trajectory"]["destination_commit"],
            "quality": record.get("quality", {}),
        }
        if repair_descriptions:
            metadata["repair_task_descriptions"] = repair_descriptions
        if task == "text-editing":
            descriptions = record.get("quality", {}).get("task_descriptions", [])
            if not descriptions:
                # Do not manufacture a reverse-shaped edit instruction from a
                # sprint title.  Missing planner descriptions make it a useful
                # trajectory, but not an aligned v2 training example.
                continue
            text = {
                **common, "task": "text-editing",
                "instruction": {"src_code": src_code, "description": descriptions},
                "response": patches, "metadata": metadata,
            }
            output["text-edit.v2"].append(text)
            source_images = [
                item["path"]
                for key in ("input_images", "src_screenshot")
                for item in record["images"].get(key, [])
            ]
            if source_images:
                output["image-edit.v2"].append({
                    "schema_version": "webcoding-image-editing-v2", **common,
                    "task": "image-editing", "instruction": descriptions,
                    "input_files": src_code, "input_images": source_images,
                    "src_screenshot": source_images, "dst_screenshot": [],
                    "patches": patches, "response": patches,
                    "conversion_status": "success", "metadata": metadata,
                })
            continue

        # Reverse text-repair receives only defective project code. Never put
        # grade text, repair instructions, or bug labels in this field.
        text = {
            **common, "task": "text-repair", "instruction": src_code,
            "response": patches, "metadata": metadata,
        }
        output["text-repair.v2"].append(text)
        source_images = [item["path"] for item in record["images"]["src_screenshot"]]
        destination_images = [item["path"] for item in record["images"]["dst_screenshot"]]
        if source_images and destination_images:
            output["image-repair.v2"].append({
                "schema_version": "webcoding-image-repair-v2", **common,
                "task": "image-repair", "instruction": "Repair the provided web project.",
                "input_files": src_code, "input_images": source_images,
                "src_screenshot": source_images, "dst_screenshot": destination_images,
                "patches": patches, "response": patches,
                "conversion_status": "success", "conversion_mode": "forward_observed_failure",
                "metadata": metadata,
            })
    return output


def append_jsonl_records(path: Path, records: list[dict[str, Any]]) -> int:
    """Append new result identities durably without rewriting costly output."""
    existing: set[str] = set()
    if path.is_file():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"existing output line {line_number} is invalid JSON: {exc}") from exc
            instance_id = item.get("instance_id") if isinstance(item, dict) else None
            if isinstance(instance_id, str):
                existing.add(instance_id)
    pending: list[dict[str, Any]] = []
    seen = set(existing)
    for record in records:
        instance_id = record.get("instance_id")
        if not isinstance(instance_id, str) or instance_id in seen:
            continue
        pending.append(record)
        seen.add(instance_id)
    if not pending:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in pending:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(pending)


def reconcile_session_records(path: Path, records: list[dict[str, Any]], session_id: str) -> int:
    """Refresh this Session's derived records while retaining prior exports as backups."""
    original = path.read_bytes() if path.is_file() else b""
    existing = [json.loads(line) for line in original.decode().splitlines() if line.strip()]
    def belongs(record):
        owner = (record.get("trajectory") or {}).get("session_id") or (record.get("quality") or {}).get("session_id")
        return owner == session_id
    if any(not belongs(record) for record in records):
        raise ValueError("Session export contains a record owned by another Session")
    others = [record for record in existing if not belongs(record)]
    updated = others + records
    ids = [record["instance_id"] for record in updated]
    if len(set(ids)) != len(ids):
        raise ValueError("Session export would duplicate a record identity")
    if existing == updated:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if original:
        backup = path.with_name(f"{path.stem}.before-{hashlib.sha256(original).hexdigest()[:12]}{path.suffix}")
        if not backup.exists():
            backup.write_bytes(original)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        for record in updated:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    previous_ids = {record["instance_id"] for record in existing}
    return sum(record["instance_id"] not in previous_ids for record in records)


def _resolved_round_commits(
    *, harness: Path, frontend: Path, grade_rounds: set[int]
) -> dict[int, str]:
    """Resolve evaluated rounds from persisted build provenance before heuristics."""
    explicit = _read_json(harness / "round_commit_map.json", {})
    build_map = _read_json(harness / "round_build_map.json", {})
    # Older minimality-disabled runs omitted the build map. Recover real Git
    # commit outputs from native tool traces, including multiple commits in a
    # build and evaluated rounds which did not build at all.
    build_map = dict(build_map) if isinstance(build_map, dict) else {}
    for round_num in sorted(grade_rounds):
        if isinstance(build_map.get(str(round_num)), dict) and build_map[str(round_num)].get("destination_commit"):
            continue
        trace = harness / "traces" / f"generator_round_{round_num}.jsonl"
        traced_commits = []
        if not trace.is_file():
            continue
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            if event.get("event") != "tool" or event.get("name") != "run_command" or event.get("ok") is not True:
                continue
            for match in re.finditer(r"^\[[^\]\n]+ ([0-9a-f]{7,40})\] (.+)$", str(event.get("output", "")), re.MULTILINE):
                commit = _git(frontend, "rev-parse", "--verify", match[1] + "^{commit}").strip()
                if _git(frontend, "show", "-s", "--format=%s", commit).strip() != match[2].strip():
                    raise ValueError(f"trace commit subject mismatch in round {round_num}")
                _git(frontend, "merge-base", "--is-ancestor", commit, "HEAD")
                if commit not in traced_commits:
                    traced_commits.append(commit)
        if traced_commits:
            parents = _git(frontend, "rev-list", "--parents", "-n", "1", traced_commits[0]).split()
            build_map[str(round_num)] = {"destination_commit": traced_commits[-1],
                "source_commit": parents[1] if len(parents) > 1 else None}
    resolved: dict[int, str] = {}
    for round_num in sorted(grade_rounds):
        mapped = explicit.get(str(round_num)) if isinstance(explicit, dict) else None
        build = build_map.get(str(round_num)) if isinstance(build_map, dict) else None
        destination = build.get("destination_commit") if isinstance(build, dict) else None
        if mapped and destination and str(mapped) != str(destination):
            raise ValueError(f"conflicting commit provenance for evaluated round {round_num}")
        if mapped or destination:
            resolved[round_num] = str(mapped or destination)

    # A later build's immutable source is the exact project state evaluated at
    # the immediately preceding round. This recovers early Generate rounds for
    # which minimality tracking began only with the first Edit sprint.
    if isinstance(build_map, dict):
        for raw_round, build in build_map.items():
            try:
                round_num = int(raw_round)
            except (TypeError, ValueError):
                continue
            if not isinstance(build, dict) or not build.get("source_commit"):
                continue
            previous = max(
                (candidate for candidate in grade_rounds if candidate < round_num),
                default=None,
            )
            if previous is not None and previous not in resolved:
                resolved[previous] = str(build["source_commit"])

    unresolved = sorted(grade_rounds - set(resolved))
    if unresolved:
        commits = agent_commits(frontend)
        if len(commits) < max(unresolved):
            raise ValueError(
                f"only {len(commits)} agent commits for evaluated rounds {unresolved}; "
                "round build provenance is incomplete"
            )
        for round_num in unresolved:
            resolved[round_num] = commits[round_num - 1]
    return resolved


def export_run(run_dir: Path, *, require_minimality: bool = True) -> list[dict[str, Any]]:
    harness = run_dir / ".harness"
    frontend = run_dir / "frontend"
    grades = _round_files(harness, "grade")
    if not grades:
        raise ValueError("no grade_round_*.json files")
    round_commit = _resolved_round_commits(
        harness=harness, frontend=frontend, grade_rounds=set(grades)
    )
    sprints = _sprint_map(harness)
    seed_manifest = _read_json(run_dir / "seed_manifest.json", {})
    forward_baseline = str(seed_manifest.get("baseline_commit") or "")
    tape_rounds = _accepted_tape_rounds(harness)
    successful = {
        round_num: grade for round_num, grade in grades.items()
        if (
            grade.get("overall_passed") is True
            and round_num in tape_rounds
            and _accepted_tape_replay_passed(
                harness,
                round_num=round_num,
                sprint_num=int(grade.get("sprint") or 0),
            )
            and _has_complete_critical_coverage(grade)
            and _trace_has_no_failed_browser_click(harness, round_num)
        )
    }
    # A sprint may have an intermediate functional pass followed by a later
    # scope/provenance repair.  Export only its terminal accepted checkpoint,
    # otherwise the same edit appears twice under one instance id.
    terminal_successful: dict[int, tuple[int, dict[str, Any]]] = {}
    for round_num, grade in successful.items():
        sprint_num = int(grade.get("sprint") or 0)
        prior = terminal_successful.get(sprint_num)
        if prior is None or round_num > prior[0]:
            terminal_successful[sprint_num] = (round_num, grade)
    records: list[dict[str, Any]] = []
    trajectory_id = f"{run_dir.name}__trajectory"

    # Reverse construction emits one edit instance with 1--7 requested task
    # types.  A forward run reaches the same state naturally over consecutive
    # accepted sprints, so preserve that trajectory as *one* edit pair instead
    # of turning every sprint into an unrelated one-task example (or, worse,
    # mislabelling later edits as generation).  Build patches incrementally:
    # a later patch is intentionally matched against the already-edited code.
    if forward_baseline:
        source_code = code_at_commit(frontend, forward_baseline)
        previous_code = source_code
        patch_chain: list[dict[str, str]] = []
        task_types: list[str] = []
        descriptions: list[dict[str, str]] = []
        accepted: list[tuple[int, int, str]] = []
        for sprint_num, (round_num, _grade) in sorted(terminal_successful.items()):
            # A missing earlier sprint means this is not one continuous
            # user-visible edit trajectory from the frozen source project.
            if sprint_num != len(accepted) + 1:
                break
            if not _strict_mutation_evidence_passed(harness, round_num, "edit", require_minimality=require_minimality):
                break
            commit = round_commit[round_num]
            source_commit = accepted[-1][2] if accepted else forward_baseline
            if require_minimality and not _minimality_pair_matches(
                harness,
                round_num,
                "edit",
                source_commit=source_commit,
                destination_commit=commit,
            ):
                break
            destination_code = code_at_commit(frontend, commit)
            sprint_types = _task_types(harness, sprint_num)
            sprint_patches = make_patches(
                previous_code, destination_code, sprint_types[0]
            )
            if apply_patches(previous_code, sprint_patches) != destination_code:
                raise ValueError("forward sprint patches do not reproduce destination code")
            patch_chain.extend(sprint_patches)
            task_types.extend(sprint_types)
            descriptions.extend(_task_descriptions(harness, sprint_num))
            accepted.append((sprint_num, round_num, commit))
            previous_code = destination_code

        if patch_chain and accepted and _edit_kind(task_types) is not None:
            first_sprint, _, _ = accepted[0]
            last_sprint, last_round, destination_commit = accepted[-1]
            if apply_patches(source_code, patch_chain) != previous_code:
                raise ValueError("aggregate forward edit patches do not reproduce destination code")
            tier, rejection_reasons = _quality_tier(
                "text-editing", patch_chain, task_types
            )
            edit_kind = _edit_kind(task_types)
            assert edit_kind is not None
            suffix = (
                f"s{first_sprint:02d}"
                if first_sprint == last_sprint
                else f"s{first_sprint:02d}_to_s{last_sprint:02d}"
            )
            records.append(_base_record(
                run_dir=run_dir,
                instance_id=f"{run_dir.name}__edit_{suffix}",
                task="text-editing", task_types=task_types,
                description=_cumulative_description(sprints, last_sprint),
                src_code=source_code, dst_code=previous_code, patches=patch_chain,
                src_images=[], dst_images=screenshots_for_round(run_dir, last_round),
                source_commit=forward_baseline, destination_commit=destination_commit,
                quality={
                    "trajectory_role": "canonical_edit",
                    "edit_kind": edit_kind,
                    "task_count": len(task_types),
                    "parent_trajectory_id": trajectory_id,
                    "source_checkpoint_id": f"seed@{forward_baseline}",
                    "target_checkpoint_id": f"accepted_sprint_{last_sprint}@{destination_commit}",
                    "checkpoint_index": last_sprint,
                    "task_descriptions": descriptions,
                    "accepted_sprints": [item[0] for item in accepted],
                    "source_checkpoint_passed": True,
                    "destination_checkpoint_passed": True,
                    "changed_files": sorted({patch["path"] for patch in patch_chain}),
                    "patches_reproduce_destination": True,
                    "tier": tier, "rejection_reasons": rejection_reasons,
                    "counterfactual_minimality": [
                        {
                            "round": item[1],
                            "status": (
                                _minimality_certificate(harness, item[1], "edit") or {}
                            ).get("status", "legacy_not_required"),
                            "artifact": f".harness/minimality_round_{item[1]}_edit.json",
                        }
                        for item in accepted
                    ],
                    "minimal_path_guidance": [
                        {"round": item[1], **_minimal_path_provenance(harness, item[1])}
                        for item in accepted
                    ],
                    **_patch_stats(patch_chain),
                },
            ))

    if not forward_baseline:
        # Accepted checkpoint transitions are the canonical development
        # history. Generate records are derived cumulative views over that
        # history, with checkpoint and complete records kept distinguishable.
        accepted_checkpoints: list[tuple[int, int, str]] = []
        for sprint_num, (round_num, _grade) in sorted(terminal_successful.items()):
            if sprint_num != len(accepted_checkpoints) + 1:
                break
            accepted_checkpoints.append(
                (sprint_num, round_num, round_commit[round_num])
            )

        planned_sprints = sorted(sprints)
        accepted_sprints = [item[0] for item in accepted_checkpoints]
        generation_complete = bool(
            planned_sprints and accepted_sprints == planned_sprints
        )

        for sprint_num, round_num, destination_commit in accepted_checkpoints:
            checkpoint_types = list(dict.fromkeys(
                task_type
                for index in range(1, sprint_num + 1)
                for task_type in _task_types(harness, index)
            ))
            records.append(_base_record(
                run_dir=run_dir,
                instance_id=f"{run_dir.name}__checkpoint_generate_s{sprint_num:02d}",
                task="text-generation",
                task_types=checkpoint_types,
                description=_cumulative_description(sprints, sprint_num),
                src_code=[],
                dst_code=code_at_commit(frontend, destination_commit),
                patches=[],
                src_images=[],
                dst_images=screenshots_for_round(run_dir, round_num),
                source_commit=None,
                destination_commit=destination_commit,
                quality={
                    "trajectory_role": "checkpoint_generate",
                    "parent_trajectory_id": trajectory_id,
                    "checkpoint_index": sprint_num,
                    "target_checkpoint_id": (
                        f"accepted_sprint_{sprint_num}@{destination_commit}"
                    ),
                    "accepted_sprints": list(range(1, sprint_num + 1)),
                    "terminal_round": round_num,
                    "destination_checkpoint_passed": True,
                },
            ))

        if generation_complete:
            last_sprint, last_round, destination_commit = accepted_checkpoints[-1]
            generation_types = list(dict.fromkeys(
                task_type
                for sprint_num in accepted_sprints
                for task_type in _task_types(harness, sprint_num)
            ))
            records.append(_base_record(
                run_dir=run_dir,
                instance_id=f"{run_dir.name}__complete_generate",
                task="text-generation",
                task_types=generation_types,
                description=_generation_description(harness, sprints, last_sprint),
                src_code=[],
                dst_code=code_at_commit(frontend, destination_commit),
                patches=[], src_images=[],
                dst_images=screenshots_for_round(run_dir, last_round),
                source_commit=None, destination_commit=destination_commit,
                quality={
                    "trajectory_role": "complete_generate",
                    "parent_trajectory_id": trajectory_id,
                    "checkpoint_index": last_sprint,
                    "target_checkpoint_id": (
                        f"accepted_sprint_{last_sprint}@{destination_commit}"
                    ),
                    "accepted_sprints": accepted_sprints,
                    "terminal_round": last_round,
                    "destination_checkpoint_passed": True,
                },
            ))

        for index in range(1, len(accepted_checkpoints)):
            previous_sprint, previous_round, previous_commit = accepted_checkpoints[index - 1]
            sprint_num, round_num, commit = accepted_checkpoints[index]
            if sprint_num != previous_sprint + 1:
                continue
            # New-policy runs must prove that the accepted incremental build is
            # an irreducible Edit; legacy runs without a policy retain their
            # historical compatibility.
            if not _strict_mutation_evidence_passed(harness, round_num, "edit", require_minimality=require_minimality):
                continue
            src_code = code_at_commit(frontend, previous_commit)
            dst_code = code_at_commit(frontend, commit)
            if require_minimality and not _minimality_pair_matches(
                harness,
                round_num,
                "edit",
                source_commit=previous_commit,
                destination_commit=commit,
            ):
                continue
            task_types = _task_types(harness, sprint_num)
            edit_kind = _edit_kind(task_types)
            if edit_kind is None:
                continue
            patches = make_patches(src_code, dst_code, task_types[0])
            if not patches:
                continue
            if apply_patches(src_code, patches) != dst_code:
                raise ValueError("edit patches do not reproduce destination code")
            tier, rejection_reasons = _quality_tier(
                "text-editing", patches, task_types
            )
            records.append(_base_record(
                run_dir=run_dir,
                instance_id=f"{run_dir.name}__edit_s{sprint_num:02d}",
                task="text-editing", task_types=task_types,
                description=_sprint_description(sprints[sprint_num]),
                src_code=src_code, dst_code=dst_code, patches=patches,
                src_images=screenshots_for_round(run_dir, previous_round),
                dst_images=screenshots_for_round(run_dir, round_num),
                source_commit=previous_commit, destination_commit=commit,
                quality={
                    "trajectory_role": "canonical_edit",
                    "edit_kind": edit_kind,
                    "task_count": len(task_types),
                    "parent_trajectory_id": trajectory_id,
                    "source_checkpoint_id": (
                        f"accepted_sprint_{previous_sprint}@{previous_commit}"
                    ),
                    "target_checkpoint_id": f"accepted_sprint_{sprint_num}@{commit}",
                    "checkpoint_index": sprint_num,
                    "source_sprint": previous_sprint,
                    "destination_sprint": sprint_num,
                    "task_descriptions": _task_descriptions(harness, sprint_num),
                    "source_checkpoint_passed": True,
                    "destination_checkpoint_passed": True,
                    "changed_files": sorted({patch["path"] for patch in patches}),
                    "patches_reproduce_destination": True,
                    "tier": tier,
                    "rejection_reasons": rejection_reasons,
                    "counterfactual_minimality": {
                        "status": (
                            _minimality_certificate(harness, round_num, "edit") or {}
                        ).get("status", "legacy_not_required"),
                        "artifact": f".harness/minimality_round_{round_num}_edit.json",
                    },
                    "minimal_path_guidance": _minimal_path_provenance(
                        harness, round_num
                    ),
                    **_patch_stats(patches),
                },
            ))

    for failed_round, grade in sorted(grades.items()):
        if not _is_real_project_failure(grade) or not _failure_has_runtime_evidence(
            harness, failed_round, grade
        ):
            continue
        sprint_num = int(grade.get("sprint") or 0)
        destination = next(
            (
                (round_num, successful_grade)
                for round_num, successful_grade in sorted(successful.items())
                if round_num > failed_round
                and int(successful_grade.get("sprint") or 0) == sprint_num
            ),
            None,
        )
        if destination is None:
            continue
        dst_round, _ = destination
        if not _strict_mutation_evidence_passed(harness, dst_round, "repair", require_minimality=require_minimality):
            continue
        src_commit, dst_commit = round_commit[failed_round], round_commit[dst_round]
        if require_minimality and not _minimality_pair_matches(
            harness,
            dst_round,
            "repair",
            source_commit=src_commit,
            destination_commit=dst_commit,
            failure_round=failed_round,
        ):
            continue
        src_code = code_at_commit(frontend, src_commit)
        dst_code = code_at_commit(frontend, dst_commit)
        labeled_grade = grade
        if _read_json(harness / "harness_state.json", {}).get("supplied_atomic_plan"):
            metadata = _read_json(harness / f"repair_metadata_round_{dst_round}.json", {})
            if metadata.get("repair_task_descriptions"):
                labeled_grade = {**grade, "repair_task_descriptions": metadata["repair_task_descriptions"]}
        repair_task_descriptions = _repair_task_descriptions(
            labeled_grade, hidden_evidence=_read_json(harness / f"hidden_oracle_evidence_round_{failed_round}.json", {}),
        )
        if not repair_task_descriptions:
            # The failure remains in the Harness trajectory, but a strict
            # dataset export must not relabel the Sprint's Edit types as Repair
            # defects or fabricate a WebCompass category.
            continue
        task_types = [item["task_type"] for item in repair_task_descriptions]
        patches = make_patches(src_code, dst_code, task_types[0])
        if not patches:
            continue
        if apply_patches(src_code, patches) != dst_code:
            raise ValueError("repair patches do not reproduce destination code")
        evidence = _confirmed_failure_evidence(grade)
        description = _repair_description(grade)
        if labeled_grade is not grade:
            description = "Repair the following reproduced project defects:\n" + "\n".join(
                f"- {item['description']}" for item in repair_task_descriptions
            )
        tier, rejection_reasons = _quality_tier(
            "text-repair", patches, task_types
        )
        records.append(_base_record(
            run_dir=run_dir,
            instance_id=f"{run_dir.name}__repair_r{failed_round:02d}_to_r{dst_round:02d}",
            task="text-repair", task_types=task_types, description=description,
            src_code=src_code, dst_code=dst_code, patches=patches,
            src_images=screenshots_for_round(run_dir, failed_round),
            dst_images=screenshots_for_round(run_dir, dst_round),
            source_commit=src_commit, destination_commit=dst_commit,
            quality={
                "trajectory_role": "natural_repair",
                "parent_trajectory_id": trajectory_id,
                "source_checkpoint_id": f"failed_round_{failed_round}@{src_commit}",
                "target_checkpoint_id": f"accepted_sprint_{sprint_num}@{dst_commit}",
                "checkpoint_index": sprint_num,
                "confirmed_failure_evidence": evidence,
                "repair_origin": "observed_edit_failure",
                "defect_injection": False,
                "taxonomy_assignment": "post_hoc_evidence_grounded",
                "repair_task_descriptions": repair_task_descriptions,
                "same_sprint_recovery": True,
                "destination_checkpoint_passed": True,
                "changed_files": sorted({patch["path"] for patch in patches}),
                "patches_reproduce_destination": True,
                "tier": tier,
                "rejection_reasons": rejection_reasons,
                "counterfactual_minimality": {
                    "status": (
                        _minimality_certificate(harness, dst_round, "repair") or {}
                    ).get("status", "legacy_not_required"),
                    "artifact": f".harness/minimality_round_{dst_round}_repair.json",
                },
                "minimal_path_guidance": _minimal_path_provenance(
                    harness, dst_round
                ),
                **_patch_stats(patches),
            },
        ))
    # Canonical Edit is the data mainline. Natural Repair follows because it is
    # a failure-to-recovery view over the same Edit attempt; cumulative Generate
    # views are emitted last and must be counted separately by trajectory_role.
    if not require_minimality:
        for record in records:
            record["quality"]["counterfactual_minimality"] = {
                "status": "skipped_by_user_policy", "required_for_export": False,
            }
    task_order = {"text-editing": 0, "text-repair": 1, "text-generation": 2}
    return sorted(records, key=lambda item: task_order.get(str(item.get("task")), 9))


def export_product_session(session_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Export accepted steps under the user's temporary minimality exemption."""
    session = _read_json(session_path, {})
    if session.get("schema_version") != "product_edit_session_v1":
        raise ValueError("expected product_edit_session_v1")
    records: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    pending = False
    prior_target = None
    for edit in session["edits"]:
        execution = edit.get("execution") or {}
        if execution.get("status") != "completed":
            pending = True
            continue
        if pending:
            raise ValueError("completed Session steps must form a continuous prefix")
        run_dir = Path(execution["workdir"])
        grade = _read_json(Path(execution["evaluation"]), {})
        if grade.get("overall_passed") is not True:
            raise ValueError(f"{edit['edit_id']}: completed step has no passing grade")
        step_records = export_run(run_dir, require_minimality=False)
        current_edits = [record for record in step_records if record["task"] == "text-editing"]
        if len(current_edits) > 1:
            raise ValueError("one Session capability must export one atomic Edit")
        if current_edits:
            current = current_edits[0]
            if prior_target is not None and current["instruction"]["src_code"] != prior_target:
                raise ValueError("Session source does not match the preceding accepted target")
            prior_target = current["reference"]["dst_code"]
        else:
            prior_target = None
        for record in step_records:
            record["instance_id"] = session["session_id"] + "__" + record["instance_id"]
            record["trajectory"].update(
                session_id=session["session_id"], edit_id=edit["edit_id"],
                source_version=edit["source_version"], target_version=edit["target_version"],
                chain_metadata=edit,
            )
            if record["task"] == "text-editing":
                label = edit["classification"]["primary"]["type"]
                record["task_type"] = [label]
                record["description"] = edit["instruction"]
                record["instruction"]["description"] = edit["instruction"]
                record["quality"]["classification"] = edit["classification"]
                record["quality"]["task_descriptions"] = [{
                    "task_type": label, "description": edit["instruction"]
                }]
                for patch in record["label_modified_files"]:
                    patch["task_type"] = label
            records.append(record)
        reports.append({
            "edit_id": edit["edit_id"], "workdir": str(run_dir),
            "execution_status": "completed", "record_count": len(step_records),
            "export_status": "exported" if step_records else "no_eligible_records",
            "evaluation": execution["evaluation"],
            "local_accepted_rounds": sorted(_accepted_tape_rounds(run_dir / ".harness")),
            "minimality_certificate": grade.get("minimality_certificate"),
        })
    generation_status = "waiting_for_complete_session"
    query_path = session_path.parent / "dataset/final_query.json"
    if session.get("generation_policy") == "each_accepted_state":
        edit_records = {row["trajectory"]["edit_id"]: row for row in records if row["task"] == "text-editing"}
        missing = []
        for index, report in enumerate(reports, 1):
            edit = session["edits"][index - 1]
            query_path = session_path.parent / "dataset/generate_queries" / f"{edit['edit_id']}.json"
            record = edit_records.get(edit["edit_id"])
            if record is None or not query_path.is_file():
                missing.append(edit["edit_id"])
                report["generation_status"] = "waiting_for_query_or_edit_export"
                continue
            query = _read_json(query_path, {})
            files = record["reference"]["dst_code"]
            content_hash = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prefix = [e["edit_id"] for e in session["edits"][:index]]
            if (query.get("schema_version") != "product-state-query-v1"
                or query.get("source_sha256") != content_hash
                or query.get("state_id") != edit["target_version"]
                or query.get("edit_ids") != prefix
                or not isinstance(query.get("instruction"), str) or not query["instruction"].strip()):
                raise ValueError(f"{edit['edit_id']}: state Generate query must match its accepted source and prefix")
            final = index == session["selection"]["edit_count"]
            generated = _base_record(
                run_dir=Path(edit["execution"]["workdir"]),
                instance_id=session["session_id"] + "__generate_" + edit["target_version"], task="text-generation",
                task_types=[], description=query["instruction"], src_code=[], dst_code=files,
                patches=[], src_images=[], dst_images=record["images"]["dst_screenshot"],
                source_commit=None, destination_commit=record["trajectory"]["destination_commit"],
                quality={"trajectory_role": "complete_generate" if final else "checkpoint_generate",
                         "session_id": session["session_id"], "accepted_edit_ids": prefix,
                         "state_id": edit["target_version"], "query_artifact": str(query_path),
                         "query_model": query["model"], "destination_checkpoint_passed": True})
            generated["trajectory"].update(session_id=session["session_id"], edit_id=edit["edit_id"],
                                           target_version=edit["target_version"])
            records.append(generated)
            report["generation_status"] = "exported"
        generation_status = "waiting_for_state_queries" if missing or not reports else "exported"
    elif len(reports) == session["selection"]["edit_count"]:
        generation_status = "waiting_for_final_query"
        if query_path.is_file():
            query = _read_json(query_path, {})
            edit_records = [record for record in records if record["task"] == "text-editing"]
            if len(edit_records) != len(reports):
                generation_status = "waiting_for_accepted_edit_exports"
            else:
                final = edit_records[-1]
                files = final["reference"]["dst_code"]
                content_hash = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if (query.get("schema_version") != "product-final-query-v1"
                    or query.get("source_sha256") != content_hash
                    or query.get("source_sha256") != session["current_state"]["sha256"]
                    or query.get("state_id") != session["current_state"]["state_id"]
                    or query.get("edit_ids") != [edit["edit_id"] for edit in session["edits"]]
                    or not isinstance(query.get("instruction"), str) or not query["instruction"].strip()):
                    raise ValueError("final Generate query must match the accepted final source and complete chain")
                records.append(_base_record(
                    run_dir=Path(session["edits"][-1]["execution"]["workdir"]),
                    instance_id=session["session_id"] + "__complete_generate", task="text-generation",
                    task_types=[], description=query["instruction"], src_code=[], dst_code=files,
                    patches=[], src_images=[], dst_images=final["images"]["dst_screenshot"],
                    source_commit=None, destination_commit=final["trajectory"]["destination_commit"],
                    quality={"trajectory_role": "complete_generate", "session_id": session["session_id"],
                             "accepted_edit_ids": query["edit_ids"], "query_artifact": str(query_path),
                             "query_model": query["model"], "destination_checkpoint_passed": True},
                ))
                generation_status = "exported"
    report = {
        "schema_version": "product-session-export-v1", "policy": "harness_accepted_minimality_skipped",
        "session_id": session["session_id"], "session": str(session_path.resolve()),
        "planned_steps": session["selection"]["edit_count"], "completed_steps": len(reports),
        "steps": reports,
        "counts": {task: sum(record["task"] == task for record in records)
                   for task in ("text-editing", "text-generation", "text-repair")},
        "generation_status": generation_status,
    }
    return records, report


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--session", type=Path)
    source.add_argument("--merge-six-outputs", type=Path, nargs="+")
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--session-report", type=Path)
    parser.add_argument("--v2-output-dir", type=Path)
    parser.add_argument("--six-output-dir", type=Path)
    args = parser.parse_args()
    if args.merge_six_outputs:
        from scripts.export_session_six_tasks import merge_batch_indexes
        merged = merge_batch_indexes(args.merge_six_outputs, args.output_jsonl)
        print(json.dumps(merged["counts"]))
        return
    if args.session:
        if args.v2_output_dir:
            parser.error("Session exports retain native classification; legacy v2 conversion is not supported")
        records, report = export_product_session(args.session)
        appended = reconcile_session_records(args.output_jsonl, records, report["session_id"])
        import asyncio
        from scripts.export_session_six_tasks import export_six_tasks
        report["six_tasks"] = asyncio.run(export_six_tasks(
            records, _read_json(args.session, {}),
            args.six_output_dir or args.output_jsonl.parent / "six_tasks",
            _REPO_ROOT / "src/orchestration/webcompass_subtask_distribution.json"))
        report_path = args.session_report or args.output_jsonl.with_suffix(".report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(report_path)
    else:
        records = export_run(args.run_dir)
        appended = append_jsonl_records(args.output_jsonl, records)
    if args.v2_output_dir:
        args.v2_output_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in to_v2_records(records).items():
            path = args.v2_output_dir / f"{name}.jsonl"
            append_jsonl_records(path, rows)
    counts = {task: sum(record["task"] == task for record in records) for task in (
        "text-generation", "text-editing", "text-repair"
    )}
    print(json.dumps({"records": len(records), "appended": appended, **counts,
                      **({"six_tasks": report["six_tasks"]["counts"]} if args.session else {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
