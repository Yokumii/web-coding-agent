"""Export a completed Harness trajectory in the construct/ records.jsonl schema."""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
from pathlib import Path
from typing import Any


CODE_EXTENSIONS = {
    ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
    ".vue", ".svelte", ".svg", ".json", ".json5", ".qml", ".ets",
    ".wxml", ".wxss",
}
IGNORED_CODE_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
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
            changes = [("", after)]
        else:
            changes = _localized_changes(before, after)
        patches.extend(
            {
                "path": path,
                "search": search,
                "replace": replace,
                "task_type": task_type,
            }
            for search, replace in changes
        )
    return patches


def _localized_changes(
    before: str, after: str, context_lines: int = 1
) -> list[tuple[str, str]]:
    """Return independent exact hunks instead of one broad file replacement."""
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    changes: list[tuple[str, str]] = []
    for group in matcher.get_grouped_opcodes(n=context_lines):
        old_start, old_end = group[0][1], group[-1][2]
        new_start, new_end = group[0][3], group[-1][4]
        search = "".join(old[old_start:old_end])
        replace = "".join(new[new_start:new_end])
        if not search or before.count(search) != 1:
            return [(before, after)]
        changes.append((search, replace))
    return changes or [(before, after)]


def apply_patches(
    src_code: list[dict[str, str]], patches: list[dict[str, str]]
) -> list[dict[str, str]]:
    code = {item["path"]: item["code"] for item in src_code}
    for patch in patches:
        path = patch["path"]
        current = code.get(path, "")
        search = patch["search"]
        if search == "" and path not in code:
            code[path] = patch["replace"]
        elif search in current:
            code[path] = current.replace(search, patch["replace"], 1)
        else:
            raise ValueError(f"patch search not found: {path}")
    return [{"path": path, "code": code[path]} for path in sorted(code)]


def _patch_stats(patches: list[dict[str, str]]) -> dict[str, Any]:
    added = sum(len(patch["replace"].splitlines()) for patch in patches)
    removed = sum(len(patch["search"].splitlines()) for patch in patches)
    return {
        "changed_file_count": len({patch["path"] for patch in patches}),
        "added_lines_in_patch_regions": added,
        "removed_lines_in_patch_regions": removed,
        "total_patch_lines": added + removed,
    }


def _quality_tier(task: str, patches: list[dict[str, str]], task_types: list[str]) -> tuple[str, list[str]]:
    stats = _patch_stats(patches)
    reasons: list[str] = []
    if any(not patch.get("search") for patch in patches):
        reasons.append("empty_search_not_reverse_compatible")
    if task == "text-editing":
        # Match the formal reverse-construction quota: task counts cycle
        # uniformly across 1..7, so a focused one-task edit is valid data.
        if not 1 <= len(task_types) <= 7:
            reasons.append("edit_task_count_outside_1_to_7")
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
        )
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
    return certificate is None or certificate.get("status") == "certified"


def _minimal_path_provenance(harness: Path, round_num: int) -> dict[str, Any]:
    """Summarize online guidance separately from post-hoc minimality proof."""
    plan_path = harness / f"minimal_path_plan_round_{round_num}.json"
    plan = _read_json(plan_path, {})
    if not isinstance(plan, dict) or plan.get("owner") != "harness":
        return {"status": "legacy_not_required"}
    ledger_path = harness / f"minimal_path_ledger_round_{round_num}.jsonl"
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
    state_path = harness / f"minimal_path_state_round_{round_num}.json"
    state = _read_json(state_path, {})
    has_applied_outcomes = any(
        item.get("decision") == "applied" for item in ledger
    )
    touched_decision = "applied" if has_applied_outcomes else "allow"
    cone = plan.get("source_change_cone") or {}
    dom = plan.get("dom_change_cone") or {}
    return {
        "status": "enforced",
        "plan_artifact": f".harness/{plan_path.name}",
        "ledger_artifact": f".harness/{ledger_path.name}",
        "state_artifact": (
            f".harness/{state_path.name}" if state_path.is_file() else None
        ),
        "initial_paths": list(cone.get("initial_paths") or []),
        "local_paths": list(cone.get("local_paths") or []),
        "dependency_paths": list(cone.get("dependency_paths") or []),
        "allowed_root_keys": list(dom.get("allowed_root_keys") or []),
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
        "images": {"src_screenshot": src_images, "dst_screenshot": dst_images},
        "llm_response": "",
        "trajectory": {
            "source_commit": source_commit,
            "destination_commit": destination_commit,
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
        # The reverse release requires every patch search to be non-empty and
        # unique. Forward records with file-creation patches remain useful
        # natural trajectories, but must not silently enter the v2-aligned set.
        if any(not patch.get("search") for patch in patches):
            continue
        common = {
            "instance_id": record["instance_id"],
            "task_type": record["task_type"],
            "page_type": "forward_harness",
            "file_manifest": _v2_file_manifest(src_code),
            "resources": [],
        }
        metadata = {
            "release_schema_reference": "webcoding-sft-v2",
            "source_project": record.get("source_project", ""),
            "task_count": len(record["task_type"]),
            "patch_count": len(patches),
            "patch_count_by_task": {
                task_type: sum(patch.get("task_type") == task_type for patch in patches)
                for task_type in record["task_type"]
            },
            "prompt_tokens": sum(len(item.get("code", "")) for item in src_code),
            "input_contract": {"max_prompt_tokens": 40000, "all_files_included": True},
            "construction_model": "qwen3.6-plus",
            "construction_source": "forward_harness",
            "source_commit": record["trajectory"]["source_commit"],
            "destination_commit": record["trajectory"]["destination_commit"],
            "quality": record.get("quality", {}),
        }
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
            source_images = [item["path"] for item in record["images"]["src_screenshot"]]
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


def export_run(run_dir: Path) -> list[dict[str, Any]]:
    harness = run_dir / ".harness"
    frontend = run_dir / "frontend"
    grades = _round_files(harness, "grade")
    commits = agent_commits(frontend)
    if not grades:
        raise ValueError("no grade_round_*.json files")
    explicit_round_commits = _read_json(harness / "round_commit_map.json", {})
    if not explicit_round_commits and len(commits) < max(grades):
        raise ValueError(
            f"only {len(commits)} agent commits for {max(grades)} evaluated rounds"
        )
    round_commit = {}
    for round_num in grades:
        mapped = explicit_round_commits.get(str(round_num))
        if mapped:
            round_commit[round_num] = str(mapped)
        elif round_num <= len(commits):
            round_commit[round_num] = commits[round_num - 1]
        else:
            raise ValueError(f"missing commit mapping for evaluated round {round_num}")
    sprints = _sprint_map(harness)
    seed_manifest = _read_json(run_dir / "seed_manifest.json", {})
    forward_baseline = str(seed_manifest.get("baseline_commit") or "")
    successful = {
        round_num: grade for round_num, grade in grades.items()
        if (
            grade.get("overall_passed") is True
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
    checkpoint_by_sprint: dict[int, tuple[int, str]] = {}

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
            if not _minimality_export_passed(harness, round_num, "edit"):
                break
            commit = round_commit[round_num]
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

        if patch_chain and accepted:
            first_sprint, _, _ = accepted[0]
            last_sprint, last_round, destination_commit = accepted[-1]
            if apply_patches(source_code, patch_chain) != previous_code:
                raise ValueError("aggregate forward edit patches do not reproduce destination code")
            tier, rejection_reasons = _quality_tier(
                "text-editing", patch_chain, task_types
            )
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

    for sprint_num, (round_num, grade) in sorted(terminal_successful.items()):
        if forward_baseline:
            # The aggregate record above is the only forward edit export.
            continue
        commit = round_commit[round_num]
        dst_code = code_at_commit(frontend, commit)
        task_types = _task_types(harness, sprint_num)
        # A forward edit starts from an accepted real project, not an empty
        # generation prompt. Export its first accepted sprint as an edit from
        # the frozen seed baseline instead of incorrectly emitting generation.
        records.append(_base_record(
            run_dir=run_dir,
            instance_id=f"{run_dir.name}__generate_s{sprint_num:02d}",
            task="text-generation",
            task_types=task_types,
            description=_cumulative_description(sprints, sprint_num),
            src_code=[], dst_code=dst_code, patches=[], src_images=[],
            dst_images=screenshots_for_round(run_dir, round_num),
            source_commit=None, destination_commit=commit,
        ))

        previous = checkpoint_by_sprint.get(sprint_num - 1)
        if previous:
            previous_round, previous_commit = previous
            src_code = code_at_commit(frontend, previous_commit)
            patch_type = task_types[0]
            patches = make_patches(src_code, dst_code, patch_type)
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
                    "task_descriptions": _task_descriptions(harness, sprint_num),
                    "source_checkpoint_passed": True,
                    "destination_checkpoint_passed": True,
                    "changed_files": sorted({patch["path"] for patch in patches}),
                    "patches_reproduce_destination": True,
                    "tier": tier,
                    "rejection_reasons": rejection_reasons,
                    "minimal_path_guidance": _minimal_path_provenance(
                        harness, round_num
                    ),
                    **_patch_stats(patches),
                },
            ))
        checkpoint_by_sprint[sprint_num] = (round_num, commit)

    for failed_round, grade in sorted(grades.items()):
        if not _is_real_project_failure(grade):
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
        if not _minimality_export_passed(harness, dst_round, "repair"):
            continue
        src_commit, dst_commit = round_commit[failed_round], round_commit[dst_round]
        src_code = code_at_commit(frontend, src_commit)
        dst_code = code_at_commit(frontend, dst_commit)
        task_types = _task_types(harness, sprint_num)
        patches = make_patches(src_code, dst_code, task_types[0])
        if not patches:
            continue
        if apply_patches(src_code, patches) != dst_code:
            raise ValueError("repair patches do not reproduce destination code")
        evidence = _confirmed_failure_evidence(grade)
        description = _repair_description(grade)
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
                "confirmed_failure_evidence": evidence,
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
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--v2-output-dir", type=Path)
    args = parser.parse_args()
    records = export_run(args.run_dir)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    )
    if args.v2_output_dir:
        args.v2_output_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in to_v2_records(records).items():
            path = args.v2_output_dir / f"{name}.jsonl"
            path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    counts = {task: sum(record["task"] == task for record in records) for task in (
        "text-generation", "text-editing", "text-repair"
    )}
    print(json.dumps({"records": len(records), **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
