from __future__ import annotations

from pathlib import Path
import json
import subprocess
from typing import Any, Literal

from src.agents._shared import expose_local_claude_skills
from src.agents.sdk_runner import (
    AgentRunStats,
    build_agent_run_stats,
    run_sdk_agent,
)
from src.config import HarnessConfig
from src.orchestration.design_contract import DesignContractContext
from src.orchestration.edit_dom_guard import repair_baseline_name
from src.orchestration.file_comm import FileComm
from src.orchestration.git_journal import ensure_repo
from src.orchestration.minimal_path_guidance import MinimalPathPolicy, plan_name
from src.orchestration.round_artifacts import RoundArtifacts
from src.orchestration.sprint_state import SprintState
from src.orchestration.target_profile import (
    target_profile_guidance,
    validate_target_submission,
)
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.prompts.grading import criterion_threshold
from src.utils.logger import get_logger

logger = get_logger(__name__)

GeneratorMode = Literal["generate", "repair"]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_CLAUDE_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"
_GENERATE_REQUIRED_READS = (
    ".harness/sprint_plan.json",
    ".harness/design_tokens.json",
    ".harness/ui_verification_plan.json",
)
_MAX_REPAIR_FILES = 4
_MAX_REPAIR_CHANGED_LINES = 1000


def _git_output(frontend_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=frontend_dir, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


async def _ensure_generator_baseline(frontend_dir: Path) -> str:
    frontend_dir.mkdir(parents=True, exist_ok=True)
    await ensure_repo(frontend_dir)
    try:
        return _git_output(frontend_dir, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "chore: baseline"],
            cwd=frontend_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return _git_output(frontend_dir, "rev-parse", "HEAD")


def _validate_generator_commits(
    frontend_dir: Path, baseline_commit: str, mode: GeneratorMode
) -> str | None:
    expected = "feat" if mode == "generate" else "fix"
    try:
        subjects = _git_output(
            frontend_dir, "log", "--format=%s", f"{baseline_commit}..HEAD"
        ).splitlines()
        status = _git_output(frontend_dir, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Git validation failed: {exc}. Initialize and use the existing frontend Git repository."
    if not any(
        subject.lower().startswith(expected + ":")
        or subject.lower().startswith(expected + "(")
        for subject in subjects
    ):
        return (
            f"No `{expected}` commit was created during this run. Validate the work, then create "
            f"an atomic `{expected}(scope): description` commit before stopping."
        )
    if status:
        return "The frontend Git worktree is not clean. Commit the remaining intended changes before stopping."
    return None


def _trace_confirms_commit(trace_path: Path, commit_hash: str, subject: str) -> bool:
    """Return true only for a native-agent trace that recorded this Git commit.

    A process can die after Git has atomically committed the model's work but
    before the build checkpoint is written.  On resume, treating HEAD as a new
    baseline makes the model create needless changes just to satisfy the commit
    hook.  The trace is the required provenance: a pre-existing commit alone
    is never sufficient to enable recovery.
    """
    if not trace_path.is_file() or not commit_hash or not subject:
        return False
    short_hash = commit_hash[:7]
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") != "tool" or event.get("name") != "run_command":
                continue
            output = str(event.get("output", ""))
            if short_hash in output and subject in output:
                return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def _recover_interrupted_commit(
    frontend_dir: Path, file_comm: FileComm, round_num: int, mode: GeneratorMode
) -> tuple[str, str] | None:
    """Find a checkpoint-missing model commit and its parent baseline."""
    try:
        head = _git_output(frontend_dir, "rev-parse", "HEAD")
        subject = _git_output(frontend_dir, "log", "-1", "--format=%s")
        parent = _git_output(frontend_dir, "rev-parse", "HEAD^")
    except (OSError, subprocess.CalledProcessError):
        return None
    expected_prefixes = ("feat:", "feat(") if mode == "generate" else ("fix:", "fix(")
    if not subject.lower().startswith(expected_prefixes):
        return None
    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    if _trace_confirms_commit(trace_path, head, subject):
        return parent, head
    return None


def _trace_written_frontend_paths(trace_path: Path) -> set[str]:
    """Return frontend paths written successfully by the native model trace.

    This is intentionally narrower than looking for arbitrary tool output: an
    automatic checkpoint may only commit paths for which the trace contains a
    successful ``write_file`` or ``apply_patch`` call with an explicit path.
    """
    if not trace_path.is_file():
        return set()
    pending_paths: list[tuple[str, str]] = []
    written: set[str] = set()
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "assistant":
                calls = ((event.get("message") or {}).get("tool_calls") or [])
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    if function.get("name") not in {"write_file", "apply_patch"}:
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    path = args.get("path") if isinstance(args, dict) else None
                    if isinstance(path, str) and path.startswith("frontend/"):
                        pending_paths.append((str(function.get("name")), path.removeprefix("frontend/")))
            elif (
                event.get("event") == "tool"
                and event.get("name") in {"write_file", "apply_patch"}
                and pending_paths
            ):
                tool_name = str(event.get("name"))
                match_index = next(
                    (index for index, (name, _path) in enumerate(pending_paths) if name == tool_name),
                    None,
                )
                if match_index is not None:
                    _name, path = pending_paths.pop(match_index)
                    if event.get("ok") is True:
                        written.add(path)
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    return written


def _trace_has_successful_validation(trace_path: Path) -> bool:
    """Require a model-recorded validation command before auto-checkpointing.

    Source writes plus a valid diff only prove that the model started work.  They
    do not prove that it reached a coherent stopping point.  A successful
    syntax/diff/build validation is the minimum trace signal that makes a
    harness-authored checkpoint honest rather than a commit of a half-built UI.
    """
    if not trace_path.is_file():
        return False
    pending_validation_calls = 0
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "assistant":
                for call in ((event.get("message") or {}).get("tool_calls") or []):
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or function.get("name") != "run_command":
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    command = str((args or {}).get("command", "")) if isinstance(args, dict) else ""
                    if any(marker in command for marker in ("git diff --check", "node --check", "npm run build", "npm test", "pnpm build", "yarn build")):
                        pending_validation_calls += 1
            elif event.get("event") == "tool" and event.get("name") == "run_command" and pending_validation_calls:
                pending_validation_calls -= 1
                if event.get("ok") is True:
                    return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return False


def _checkpoint_interrupted_model_work(
    frontend_dir: Path, file_comm: FileComm, workdir: Path, round_num: int, mode: GeneratorMode,
) -> str | None:
    """Atomically checkpoint a *previously model-written* uncommitted edit.

    This recovery never changes product source.  It is deliberately available
    only after a prior model attempt has produced the forward-edit scope. The
    harness independently validates the exact source diff before committing;
    browser evaluation then determines whether the interrupted implementation
    is a natural repair source or an accepted edit.
    """
    if mode != "generate" or not (workdir / "seed_manifest.json").is_file():
        return None
    if _validate_edit_scope(workdir, round_num) is not None:
        return None
    try:
        changed_paths = set(filter(None, _git_output(frontend_dir, "diff", "--name-only").splitlines()))
        if not changed_paths:
            return None
        subprocess.run(["git", "diff", "--check"], cwd=frontend_dir, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    if not changed_paths.issubset(_trace_written_frontend_paths(trace_path)):
        return None
    # A timeout after a few writes is an infrastructure interruption, not yet a
    # natural completed edit.  Do not manufacture a repair seed by committing
    # that partial state.  The trace must show that the model itself reached a
    # successful syntax/diff/build validation before recovery may checkpoint
    # its untouched diff.
    if not _trace_has_successful_validation(trace_path):
        return None
    # A syntax check is cheap for static seeds and prevents checkpointing a
    # visibly broken script merely because the model ran out of tool calls.
    try:
        for path in sorted(changed_paths):
            if path.endswith(".js"):
                subprocess.run(["node", "--check", path], cwd=frontend_dir, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run(["git", "add", "--all"], cwd=frontend_dir, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        prefix = "feat" if mode == "generate" else "fix"
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m",
             f"{prefix}(recovery): checkpoint interrupted model implementation"],
            cwd=frontend_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        commit = _git_output(frontend_dir, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return None

    file_comm.write_build_log(
        "# Interrupted model implementation recovery\n\n"
        f"Round: {round_num}\nMode: {mode}\nCommit: {commit}\n\n"
        "The native model trace created the committed frontend diff, but exhausted its "
        "tool-call budget before its own checkpoint. The harness verified `git diff --check` "
        "and JavaScript syntax, then committed the unchanged model-written diff. "
        "No product source was synthesized or modified by recovery.\n"
    )
    file_comm.append_progress_entry(
        f"## Round {round_num} recovery\n\n"
        f"Harness checkpointed the trace-proven model diff at `{commit[:12]}` after tool-budget exhaustion."
    )
    (file_comm.dir / f"recovery_commit_round_{round_num}.json").write_text(
        json.dumps({
            "status": "ok", "commit_mode": "harness_checkpoint", "round": round_num,
            "commit": commit, "source_change_author": "native_model_trace",
            "source_files": sorted(changed_paths), "cost_status": "precheckpoint_model_cost_unknown",
        }, indent=2) + "\n", encoding="utf-8"
    )
    return commit


def _is_harness_checkpoint_for_round(
    frontend_dir: Path, file_comm: FileComm, round_num: int, mode: GeneratorMode,
) -> bool:
    """Recognize only the exact, trace-proven checkpoint created above."""
    path = file_comm.dir / f"recovery_commit_round_{round_num}.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        head = _git_output(frontend_dir, "rev-parse", "HEAD")
        subject = _git_output(frontend_dir, "log", "-1", "--format=%s")
        committed_paths = set(filter(None, _git_output(frontend_dir, "diff", "--name-only", "HEAD^..HEAD").splitlines()))
        status = _git_output(frontend_dir, "status", "--porcelain")
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError):
        return False
    expected_prefix = "feat" if mode == "generate" else "fix"
    return (
        metadata.get("status") == "ok"
        and metadata.get("commit_mode") == "harness_checkpoint"
        and metadata.get("round") == round_num
        and metadata.get("commit") == head
        and metadata.get("source_change_author") == "native_model_trace"
        and set(metadata.get("source_files") or []) == committed_paths
        and subject == f"{expected_prefix}(recovery): checkpoint interrupted model implementation"
        and not status
    )


def _validate_repair_scope(frontend_dir: Path, baseline_commit: str) -> str | None:
    """Reject broad repair commits before they become accepted trajectory states."""
    try:
        output = _git_output(
            frontend_dir, "diff", "--numstat", f"{baseline_commit}..HEAD", "--"
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Repair scope validation failed: {exc}."
    changed_files = 0
    changed_lines = 0
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if Path(path).name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}:
            continue
        changed_files += 1
        if added.isdigit():
            changed_lines += int(added)
        if removed.isdigit():
            changed_lines += int(removed)
    if changed_files > _MAX_REPAIR_FILES or changed_lines > _MAX_REPAIR_CHANGED_LINES:
        return (
            "Repair diff is too broad for an atomic repair: "
            f"{changed_files} source files and {changed_lines} changed lines; allowed maximum is "
            f"{_MAX_REPAIR_FILES} files and {_MAX_REPAIR_CHANGED_LINES} changed lines. "
            "Reduce the committed diff to the evaluator-confirmed defect only. Preserve all "
            "unrelated code and formatting byte-for-byte, then create a corrective fix commit."
        )
    return None


def _is_forward_static_seed(workdir: Path) -> bool:
    manifest = workdir / "seed_manifest.json"
    if not manifest.is_file():
        return False
    try:
        import json
        source = Path(json.loads(manifest.read_text(encoding="utf-8"))["source_frontend"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (source / "index.html").is_file() and not (source / "package.json").is_file()


def _validate_generator_runnable_files(frontend_dir: Path, workdir: Path) -> str | None:
    if _is_forward_static_seed(workdir):
        return None
    package_json = frontend_dir / "package.json"
    if not package_json.is_file():
        return (
            "The frontend is missing package.json. Create a runnable frontend package with "
            "at least a dev script, validate it, and commit it inside frontend/.git."
        )
    return None


def _validate_edit_scope(
    workdir: Path,
    round_num: int,
    *,
    required: bool = False,
    baseline_filename: str | None = None,
) -> str | None:
    """Make the declared edit boundary an explicit generator deliverable."""
    if not required and not (workdir / "seed_manifest.json").is_file():
        return None
    path = workdir / ".harness" / f"edit_scope_round_{round_num}.json"
    if not path.is_file():
        return f"Scoped edit/repair requires `{path.relative_to(workdir)}` before stopping."
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"Forward edit scope is not valid JSON: {exc}"
    roots = payload.get("allowed_root_keys") if isinstance(payload, dict) else None
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        return "Forward edit scope must contain a string list `allowed_root_keys`."
    if len(set(roots)) != len(roots):
        return "Forward edit scope root keys must be distinct."
    harness_baseline = payload.get("baseline") if isinstance(payload, dict) else None
    if baseline_filename is None and isinstance(harness_baseline, str):
        candidate = Path(harness_baseline)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.parent.as_posix() not in {".", ".harness"}
        ):
            return "Forward edit scope contains an invalid harness baseline reference."
        baseline_filename = candidate.name
    baseline_path = workdir / ".harness" / (
        baseline_filename or "edit_dom_baseline.json"
    )
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        valid_roots = {str(item["key"]) for item in baseline.get("roots", [])}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"Forward edit baseline roots are unavailable: {exc}"
    unknown = sorted(set(roots) - valid_roots)
    if unknown:
        return "Forward edit scope contains unknown baseline roots: " + ", ".join(unknown)
    if baseline.get("version") == 3:
        target_routes = payload.get("target_routes")
        protected_routes = payload.get("protected_routes")
        if (
            not isinstance(target_routes, list)
            or not target_routes
            or not all(isinstance(item, str) for item in target_routes)
            or not isinstance(protected_routes, list)
            or not all(isinstance(item, str) for item in protected_routes)
        ):
            return "Multi-route edit scope requires string lists `target_routes` and `protected_routes`."
        target_set = set(target_routes)
        protected_set = set(protected_routes)
        if target_set & protected_set:
            return "Multi-route edit scope target and protected routes must be disjoint."
        root_routes = {
            str(item.get("key")): str(item.get("route", ""))
            for item in baseline.get("roots", [])
            if isinstance(item, dict) and item.get("key")
        }
        counts: dict[str, int] = {}
        for root in roots:
            route = root_routes.get(root, "")
            if not route or route not in target_set or route in protected_set:
                return "Multi-route edit scope may only allow roots owned by target routes."
            counts[route] = counts.get(route, 0) + 1
        if any(count > 2 for count in counts.values()):
            return "Multi-route edit scope may declare at most two roots per target route."
    elif len(roots) > 2:
        return "Forward edit scope may declare at most two distinct root keys."
    if not isinstance(payload.get("allow_new_roots", False), bool):
        return "Forward edit scope field `allow_new_roots` must be boolean."
    return None


def _is_scope_contract_only_repair(grades: dict[str, Any]) -> bool:
    """Whether a repair needs only the forward-edit declaration artifact."""
    if grades.get("edit_scope_audit") != "fail":
        return False
    if grades.get("sprint_passed") is not True:
        return False
    if grades.get("regression_passed") is not False:
        return False
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict) or str(check.get("status", "")).lower() != "pass":
            return False
    for criterion in grades.get("target_exit_criteria_results", []):
        if not isinstance(criterion, dict) or criterion.get("passed") is not True:
            return False
    return True


def _make_generator_stop_hook(
    frontend_dir: Path, baseline_commit: str, mode: GeneratorMode, workdir: Path, round_num: int,
    target_profile: dict[str, Any] | None = None, scope_contract_only: bool = False,
):
    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        error = _validate_generator_runnable_files(frontend_dir, workdir)
        if error is None:
            error = validate_target_submission(frontend_dir, target_profile)
        if error is None and not scope_contract_only:
            error = _validate_generator_commits(frontend_dir, baseline_commit, mode)
        if error is None:
            is_forward = (workdir / "seed_manifest.json").is_file()
            repair_baseline = workdir / ".harness" / repair_baseline_name(round_num)
            error = _validate_edit_scope(
                workdir,
                round_num,
                required=(is_forward or (mode == "repair" and repair_baseline.is_file())),
                baseline_filename=(
                    None if is_forward else repair_baseline_name(round_num)
                ),
            )
        if error is None and mode == "repair":
            error = _validate_repair_scope(frontend_dir, baseline_commit)
        if error:
            return {"decision": "block", "reason": error, "stopReason": error}
        return {"continue_": True}
    return _hook


def _ensure_local_claude_skills(workdir: Path) -> None:
    """将仓库内置 skills 暴露到 generator 的工作目录。"""
    expose_local_claude_skills(workdir, _LOCAL_CLAUDE_SKILLS_DIR)


def _describe_failures(grades: dict[str, Any], sprint_context: dict[str, Any]) -> str:
    """将上一轮失败项整理成 repair prompt 可直接引用的 Markdown 列表。

    数据来源有三类：

    * ``criteria`` 中低于阈值的评分项；
    * ``ui_checks`` 中状态为 ``fail`` 或 ``partial`` 的检查项；
    * ``target_exit_criteria_results`` 中 ``passed=False`` 的退出条件。

    如果没有识别到失败项，则返回一条保守的兜底说明。
    """
    lines: list[str] = []

    criteria = grades.get("criteria") or {}
    failed_criteria: list[tuple[str, dict[str, Any]]] = []
    if isinstance(criteria, dict):
        for name, payload in criteria.items():
            if not isinstance(payload, dict):
                continue
            threshold = criterion_threshold(name, default=6.0)
            score = payload.get("score")
            if isinstance(score, bool):
                continue
            if isinstance(score, (int, float)) and score < threshold:
                failed_criteria.append((name, payload))

    if failed_criteria:
        lines.append("### Failed criteria")
        for name, payload in failed_criteria:
            notes = str(payload.get("notes", "") or "").strip()
            lines.append(
                f"- **{name}** (score {payload.get('score')}): {notes}"
            )

    target_ids: set[str] = set()
    for fid in sprint_context.get("feature_ids", []) or []:
        text = str(fid).strip()
        if text:
            target_ids.add(text)

    failed_checks = [
        check
        for check in (grades.get("ui_checks") or [])
        if isinstance(check, dict)
        and str(check.get("status", "")).strip().lower() in {"fail", "partial"}
        and (not target_ids or str(check.get("feature_id", "")).strip() in target_ids)
    ]
    if failed_checks:
        if lines:
            lines.append("")
        lines.append("### Failed UI checks")
        for check in failed_checks:
            fid = check.get("feature_id", "?")
            status = check.get("status", "")
            notes = str(check.get("notes", "") or check.get("task", "") or "").strip()
            lines.append(f"- {fid} [{status}]: {notes}")

    failed_exits = [
        result
        for result in (grades.get("target_exit_criteria_results") or [])
        if isinstance(result, dict)
        and result.get("passed") is False
        and (not target_ids or str(result.get("feature_id", "")).strip() in target_ids)
    ]
    if failed_exits:
        if lines:
            lines.append("")
        lines.append("### Failed exit criteria")
        for result in failed_exits:
            fid = result.get("feature_id", "?")
            notes = str(result.get("notes", "") or result.get("criterion", "") or "").strip()
            lines.append(f"- {fid}: {notes}")

    regressions = [
        str(item).strip() for item in (grades.get("regressions_found") or [])
        if str(item).strip()
    ]
    instructions = [
        str(item).strip() for item in (grades.get("repair_instructions") or [])
        if str(item).strip()
    ]
    if regressions:
        if lines:
            lines.append("")
        lines.append("### Reproduced regressions")
        lines.extend(f"- {item}" for item in regressions)
    if instructions:
        if lines:
            lines.append("")
        lines.append("### Required repair actions")
        lines.extend(f"- {item}" for item in instructions)

    return "\n".join(lines) if lines else "(no specific failures found in previous grades)"


def _build_generator_prompt(
    *,
    mode: GeneratorMode,
    file_comm: FileComm,
    round_num: int,
    sprint_num: int,
    sprint_context: dict,
    accepted_sprints: dict,
    resume_uncommitted_work: bool = False,
    recovered_commit: str | None = None,
) -> str:
    """构造 generator 单轮提示词，按 generate/repair 两种模式切换细节。"""
    round_artifacts = RoundArtifacts(file_comm, round_num)
    design_contract = DesignContractContext.load(file_comm)
    accepted = accepted_sprints.get("accepted", [])
    target_profile = file_comm.read_target_profile()
    prior_grades = file_comm.read_grades(round_num - 1) if mode == "repair" else None
    scope_contract_only = isinstance(prior_grades, dict) and _is_scope_contract_only_repair(prior_grades)
    target_guidance = target_profile_guidance(target_profile)
    is_forward_edit = (file_comm.dir.parent / "seed_manifest.json").is_file()
    repair_frame_path = file_comm.dir / repair_baseline_name(round_num)
    has_repair_frame = mode == "repair" and repair_frame_path.is_file()
    minimal_path_ref = f".harness/{plan_name(round_num)}"
    minimal_path_owned = (file_comm.dir / plan_name(round_num)).is_file()
    feature_ids = ", ".join(sprint_context.get("feature_ids", []))
    common_lines = [
        f"Mode: {mode}\n",
        f"Round: {round_num}\n"
        f"Sprint: {sprint_num}\n"
        f"Sprint Title: {sprint_context.get('title')}\n"
        f"Target Feature IDs: {feature_ids}\n"
        f"Accepted Sprints: {accepted}\n"
    ]
    if resume_uncommitted_work:
        common_lines.extend([
            "\nInterrupted-attempt recovery:\n",
            "- A prior invocation for this exact sprint already left intended uncommitted changes in `frontend/`.\n",
            "- FIRST inspect `git -C frontend diff --stat` and the targeted diff. Do not reread whole source files, restart the design, or reopen earlier accepted sprint scope.\n",
            "- Verify the targeted diff against `.harness/ui_verification_plan.json`. If a required selector or behavior is missing, finish only that missing work with a focused patch before validation; do not restart the design or read unrelated source.\n",
            "- Once the action contract is complete, keep only changes needed for this sprint; validate them, update the required harness artifacts, and make the required atomic commit.\n",
        ])
    if recovered_commit:
        common_lines.extend([
            "\nVerified interrupted-commit recovery:\n",
            f"- The native-model trace already records the current atomic commit `{recovered_commit[:12]}` for this exact sprint.\n",
            "- Do NOT call tools, edit files, or create another commit. Respond immediately that this committed implementation is ready for browser evaluation.\n",
        ])
    if minimal_path_owned:
        common_lines.extend([
            "\nHarness-owned minimal-path channel:\n",
            f"- FIRST read `{minimal_path_ref}`. The harness already materialized "
            f"`.harness/edit_scope_round_{round_num}.json` and a live minimal-path state; do not "
            "create, copy, or edit those harness-owned artifacts.\n",
            "- Inspect only `source_change_cone.initial_paths` first. The tool layer requires a "
            "successful read of that exact file before it accepts an exact patch.\n",
            "- Treat `route_scope.target_routes` as the only page owners in scope. "
            "`cross_route_shared_paths` are closed because they also affect non-target pages; "
            "`off_target_paths` are closed outright. A shared file opens only when every owning "
            "route is targeted by this sprint.\n",
            "- After every successful source mutation, run the smallest applicable syntax, diff, "
            "build, or test validation. Only then can a path connected by a recorded dependency "
            "edge be unlocked; protected and unplanned new source paths remain rejected.\n",
            "- If the initial path completes the contract, do not widen. A successful validation "
            "after the latest mutation is required before commit.\n",
            "- Existing source overwrites are rejected. Use exact, unique patches within the plan's "
            "line and touched-file budgets. Reads, applied mutations, validation transitions, denials, "
            "and dependency widening are recorded in the minimal-path ledger.\n",
            "- This is an execution policy enforced by the harness. The later counterfactual "
            "certificate remains an independent final check.\n",
        ])
    if is_forward_edit and not minimal_path_owned:
        try:
            baseline = json.loads((file_comm.dir / "edit_dom_baseline.json").read_text(encoding="utf-8"))
            root_keys = [str(item["key"]) for item in baseline.get("roots", [])]
        except (OSError, ValueError, KeyError, TypeError):
            root_keys = []
        common_lines.extend([
            "\nForward edit safety contract:\n",
            f"- Before your final commit, write `.harness/edit_scope_round_{round_num}.json`.\n",
            "- It must contain `allowed_root_keys` (at most two exact root keys from the baseline) and `allow_new_roots` (boolean).\n",
            f"- The ONLY valid baseline root keys are: {', '.join(root_keys) or '(unavailable; read the baseline file)'}. Copy one or two of these exact strings; file paths such as `frontend/index.html` are invalid.\n",
            "- Decide the new-root policy from the implementation you are about to commit: set `allow_new_roots` to true when the sprint intentionally adds a top-level interactive surface (for example a sidebar, modal, or floating action region); otherwise set it to false. Do not leave it false merely because the new surface is visually associated with an allowed baseline root.\n",
            "- Write this scope artifact before the first frontend source edit, not at the end of the turn. After source edits, run the smallest applicable validation and commit immediately; do not spend late tool calls rereading build logs or planning artifacts.\n",
            "- The harness independently rejects semantic DOM/ARIA changes outside this declared scope.\n",
            "- If the frozen seed is a plain HTML/CSS/JS site, do not create package.json, lockfiles, dev servers, or dependencies; the harness serves it statically.\n",
        ])
    elif has_repair_frame and not minimal_path_owned:
        try:
            baseline = json.loads(repair_frame_path.read_text(encoding="utf-8"))
            root_keys = [str(item["key"]) for item in baseline.get("roots", [])]
        except (OSError, ValueError, KeyError, TypeError):
            root_keys = []
        common_lines.extend([
            "\nRepair semantic safety contract:\n",
            f"- FIRST write `.harness/edit_scope_round_{round_num}.json` before editing frontend source.\n",
            "- It must contain `allowed_root_keys` (at most two exact roots from the failed-source baseline) and `allow_new_roots` (boolean).\n",
            f"- The ONLY valid failed-source roots are: {', '.join(root_keys) or '(unavailable; read the repair baseline file)'}.\n",
            f"- Read `.harness/{repair_baseline_name(round_num)}` only to choose that narrow footprint. The harness protects every other semantic DOM/ARIA surface.\n",
        ])

    if mode == "generate":
        required_reads = list(_GENERATE_REQUIRED_READS)
        if minimal_path_owned:
            required_reads.append(minimal_path_ref)
        if target_profile:
            required_reads.append(".harness/target_profile.json")
        required_reads.extend(design_contract.required_refs())
        required_reads.extend(round_artifacts.previous_existing_refs())
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        deliverables = "\n".join(f"- {item}" for item in sprint_context.get("deliverables", []))
        exit_criteria = "\n".join(f"- {item}" for item in sprint_context.get("exit_criteria", []))
        design_guidance = design_contract.generator_guidance()
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        mode_lines = [
            f"Sprint Goal: {sprint_context.get('goal')}\n"
            f"Deliverables:\n{deliverables}\n"
            f"Exit Criteria:\n{exit_criteria}\n"
            f"Required Reads:\n{required_reads_text}\n\n"
            "The sprint goal, target feature IDs, deliverables, and accepted-sprint state are already "
            "included above. Do not reread feature_list.json or accepted_sprints.json unless a concrete "
            "conflict requires it.\n"
            f"{design_guidance_block}"
            f"{target_guidance}\n"
            f"Implement only sprint {sprint_num}.\n"
            f"Set up or update the runnable browser preview in `frontend/`.\n"
            "For an existing project, inspect only the relevant line range or selector before editing; "
            "do not repeatedly reread a truncated whole source file. Preserve its current stack and "
            "use one focused patch per affected file.\n"
            f"Do not implement future sprint functionality or unrelated refactors.\n"
            f"If previous-round feedback or grades are present, read them to preserve accepted work, "
            f"avoid regressions, and carry forward non-blocking polish notes without re-opening already accepted sprint scope.\n"
        ]
        allowed_examples = (
            "Allowed examples: `ls frontend`, `npm create vite@latest frontend -- --template react`, "
            "`npm install --prefix frontend`, `npm --prefix frontend run build`, "
            "`cd frontend && npm run build`, `find frontend/src -type f | head -40`.\n"
        )
        build_log_instruction = (
            "When done, update `.harness/build_log.md` with round, sprint, mode, implemented features, "
            "and a short summary of what was completed.\n"
        )
    else:
        feedback_round = round_num - 1
        previous_artifacts = RoundArtifacts(file_comm, feedback_round)
        previous_grades = file_comm.read_grades(feedback_round)
        if previous_grades is None:
            raise RuntimeError(
                f"Generator repair mode requires {previous_artifacts.grade_ref} "
                f"from the previous round, but it was not found. The previous round may "
                f"have crashed before writing grades."
            )
        failures_text = _describe_failures(previous_grades, sprint_context)
        design_guidance = design_contract.generator_guidance()
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        # The sprint goal and normalized failure findings are already inlined
        # below.  Making Qwen reread their source artifacts costs several
        # long tool turns on every natural repair, yet adds no new evidence.
        # Scope plus the browser-selector contract are the only mandatory
        # repair reads; source inspection then stays targeted to the failure.
        required_reads = [
            *(
                [minimal_path_ref, f".harness/edit_scope_round_{round_num}.json"]
                if minimal_path_owned
                else [f".harness/edit_scope_round_{feedback_round}.json"]
                if is_forward_edit
                else [f".harness/{repair_baseline_name(round_num)}"]
                if has_repair_frame
                else []
            ),
            ".harness/ui_verification_plan.json",
            # Design-stage files are not duplicated in the repair prompt and
            # must remain available when a visual/regression repair depends on
            # their placement or responsive contract.
            *design_contract.required_refs(),
        ]
        minimality = previous_grades.get("minimality_certificate") or {}
        edit_certificate = minimality.get("edit") if isinstance(minimality, dict) else None
        if isinstance(edit_certificate, dict) and edit_certificate.get("status") == "non_minimal":
            required_reads.append(
                str(edit_certificate.get("artifact") or f".harness/minimality_round_{feedback_round}_edit.json")
            )
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        if minimal_path_owned:
            scope_first_action = (
                f"FIRST ACTION: read `{minimal_path_ref}` and inspect only its single initial "
                "source path. The scope is already computed and immutable.\n"
            )
            scope_preservation_guidance = (
                "Follow the harness-selected state transition returned by the tools: inspect, make "
                "one exact patch, validate, and only then follow a recorded dependency edge when "
                "the contract still requires it. Do not cross from a target route into shared or "
                "off-target page source. If a mutation is denied, use its returned next "
                "action instead of expanding to an unrelated file.\n"
            )
        elif is_forward_edit:
            scope_first_action = (
                f"FIRST ACTION: copy `.harness/edit_scope_round_{feedback_round}.json` to "
                f"`.harness/edit_scope_round_{round_num}.json` before any investigation. This is a "
                "required trajectory artifact, not a conclusion about the repair.\n"
            )
            scope_preservation_guidance = (
                "Preserve the previous edit scope whenever the repair remains within its declared "
            "surfaces/new-root policy. Do not replace a valid allow_new_roots contract with a narrower "
            "one merely because the changed element is visually near a different page region. If the "
            "previous grade's scope audit reports an undeclared new root, correct the copied scope "
            "artifact before your final commit: retain the declared baseline roots and set "
            "allow_new_roots to true only when the repair still genuinely needs that root. A new root "
            "can NEVER be added to allowed_root_keys because that list only permits baseline keys. If "
            "all product checks passed and scope audit is the only failure, do not modify frontend source "
            "or invent an empty Git commit: update only the scope artifact and required logs.\n"
            )
        elif has_repair_frame:
            scope_first_action = (
                f"FIRST ACTION: write `.harness/edit_scope_round_{round_num}.json` from the "
                "failed-source semantic roots listed above, before investigating or editing source.\n"
            )
            scope_preservation_guidance = (
                "Keep the repair inside the newly declared failed-source roots. Do not change, remove, "
                "or restyle semantic surfaces outside that repair footprint.\n"
            )
        else:
            scope_first_action = (
                "The failed source did not render, so no DOM/ARIA repair frame is available; "
                "start from the exact startup evidence and keep the source diff atomic.\n"
            )
            scope_preservation_guidance = (
                "Because the source could not render, preserve every unrelated file and line "
                "byte-for-byte and change only the startup defect.\n"
            )
        mode_lines = [
            "Repair Scope: Fix evaluator-reported issues for the current sprint only\n"
            f"{scope_first_action}"
            f"Required minimal reads:\n{required_reads_text}\n\n"
            f"{design_guidance_block}"
            f"{target_guidance}\n"
            "## Previous evaluation findings\n\n"
            f"{failures_text}\n\n"
            "## Your task\n"
            "Address every failure above. Do not stop until each one is fixed. "
            "There is no self-report file; the next evaluation round verifies your work.\n"
            "Use the inlined findings and grade/feedback as the primary evidence; do not reread their "
            "files unless their inlined content is truncated. Read the previous "
            "evaluator trace only when a finding names a failed normal browser_click or an exact "
            "runtime error; then inspect only the matching trace lines. A failed normal browser_click "
            "is a reproduced usability defect: fix its cause; do not treat a forced or programmatic "
            "click as a substitute. A merely partial or unverified check is not by itself proof of a defect.\n"
            "For a reported source or runtime defect, start from the exact failing behavior and use "
            "targeted line-range reads (for example `sed -n`) around the implicated handler or markup; "
            "do not repeatedly reread a truncated whole file. Before committing JavaScript changes, "
            "verify the edited script is syntactically valid (for example `node --check`) and preserve "
            "the previously working behavior outside the reported defect.\n"
            "For interaction state that closes, dismisses, cancels, or resets incorrectly, trace any "
            "pending asynchronous work (debounce timers, delayed callbacks, promises, animation completion, "
            "or stale requests) that can reapply the old state. Cancel or invalidate that work before "
            "adding redundant event handlers or compatibility fallbacks.\n"
            "When a browser API such as Web Share or clipboard is the reported failure, do not await an "
            "unbounded native prompt before updating the user-visible feedback state. Give the aria-live "
            "feedback synchronously or through a bounded fallback, then preserve the native API as a "
            "best-effort enhancement.\n"
            "After the required scope declaration, make the smallest repair, commit it, and update the build "
            "log/progress artifacts. These required artifacts take priority over additional exploratory "
            "tool calls when turns are limited.\n"
            "Visual evidence is captured independently by the harness in both top and scrolled states. "
            "Never alter required product visibility or interaction behavior merely to make a screenshot show a control.\n"
            f"{scope_preservation_guidance}"
            "Fix ONLY the issues needed for sprint acceptance or regression recovery.\n"
            "Use localized patches and preserve untouched code exactly; broad rewrites or "
            "formatting churn make the repair unusable as training data. Normally touch no "
            "more than four source files.\n"
            "Do not implement new features from future sprints.\n"
            "Do not start work for the next sprint.\n"
        ]
        allowed_examples = (
            f"Allowed examples: `ls frontend/src`, `grep -n \"pattern\" frontend/src/App.jsx`, "
            f"`npm install --prefix frontend`, `npm --prefix frontend run build`, "
            f"`cd frontend && npm run build`, `find frontend/src -type f | head -40`.\n"
        )
        build_log_instruction = (
            "When done, update `.harness/build_log.md` with round, sprint, mode, addressed issues, "
            "and a short summary of what was repaired.\n"
        )

    common_tail = (
        "The native OpenAI runner has no `.claude/skills/` directory. Do not attempt to read that path.\n"
        "Use paths relative to the workdir when calling tools; do not use absolute paths.\n"
        "For Bash, command chains and pipelines are allowed when each segment stays inside the workdir.\n"
        "For every planner-authored UI action, implement the exact stable selector specified in `.harness/ui_verification_plan.json`; these selectors are part of the acceptance contract, not optional test metadata.\n"
        "Do not use background execution, redirection, or command substitution such as `&`, `>`, `<`, `$(`, or backticks.\n"
        "For package-manager and build commands, target `frontend/` explicitly with "
        "`npm --prefix frontend ...` or `cd frontend && ...`; never run `npm run build` from the workdir root.\n"
        f"{allowed_examples}"
        f"{build_log_instruction}"
        "Also append a short progress entry to `.harness/progress.md`.\n"
        "Treat `.` as the workdir root."
    )

    return "".join(common_lines + mode_lines) + common_tail


def _validate_generator_outputs(file_comm: FileComm, workdir: Path, result_summary: str) -> None:
    """校验 generator 的最低交付物，避免空跑后继续后续阶段。"""
    frontend_dir = workdir / "frontend"
    package_json = frontend_dir / "package.json"

    if frontend_dir.exists() and (package_json.exists() or _is_forward_static_seed(workdir)):
        target_error = validate_target_submission(
            frontend_dir, file_comm.read_target_profile()
        )
        if target_error is None:
            return
        raise RuntimeError(target_error)

    if result_summary and not file_comm.read_build_log():
        file_comm.write_build_log(result_summary)

    if not frontend_dir.exists():
        existing_dirs = sorted(
            path.relative_to(workdir).as_posix()
            for path in workdir.iterdir()
            if path.is_dir() and path.name != ".harness"
        )
        raise RuntimeError(
            "Generator completed without creating the expected frontend directory "
            f"('frontend'). Found directories: {existing_dirs or 'none'}."
        )

    raise RuntimeError(
        "Generator created 'frontend/' but it is missing 'package.json'. "
        "An empty frontend directory cannot serve a dev server, so the round "
        "is treated as a failed build instead of waiting 90s for the dev "
        "server to time out."
    )


async def run_generator(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    mode: GeneratorMode,
) -> AgentRunStats:
    """运行 generator，并返回统一的执行统计信息。"""
    logger.info(
        f"[bold green]Generator[/] starting mode={mode} round={round_num} sprint={sprint_num}"
    )
    # These long Claude-oriented skill documents are useful to the Claude SDK,
    # but Qwen repeatedly reads them verbatim and spends repair turns before
    # inspecting the actual failing project.  Sprint/design artifacts are the
    # authoritative guidance for the native OpenAI runner.
    if config.agent_runtime.strip().lower() != "openai":
        _ensure_local_claude_skills(workdir)

    frontend_dir = workdir / "frontend"
    baseline_commit = await _ensure_generator_baseline(frontend_dir)
    if _is_harness_checkpoint_for_round(frontend_dir, file_comm, round_num, mode):
        logger.info(
            "[bold green]Generator[/] found verified harness checkpoint for round %s; proceeding to evaluation without a duplicate model call.",
            round_num,
        )
        return AgentRunStats(
            cost_usd=0.0,
            duration_ms=0,
            duration_api_ms=0,
            token_usage={},
            usage={"recovery": "harness_checkpoint", "precheckpoint_model_cost": "unknown"},
            model_usage={},
        )
    recovered = _recover_interrupted_commit(frontend_dir, file_comm, round_num, mode)
    recovered_commit = None
    if recovered is not None:
        baseline_commit, recovered_commit = recovered
        logger.info(
            "[bold green]Generator[/] recovered trace-backed commit %s; requesting only completion confirmation.",
            recovered_commit[:12],
        )
    resume_uncommitted_work = bool(_git_output(frontend_dir, "status", "--porcelain"))
    if resume_uncommitted_work:
        checkpoint = _checkpoint_interrupted_model_work(
            frontend_dir, file_comm, workdir, round_num, mode,
        )
        if checkpoint is not None:
            logger.info(
                "[bold green]Generator[/] checkpointed trace-proven interrupted model work at %s; no new model call made.",
                checkpoint[:12],
            )
            return AgentRunStats(
                cost_usd=0.0,
                duration_ms=0,
                duration_api_ms=0,
                token_usage={},
                usage={"recovery": "harness_checkpoint", "precheckpoint_model_cost": "unknown"},
                model_usage={},
            )

    if recovered_commit is not None:
        # The native trace already proves this exact clean HEAD was committed
        # by the model.  Asking the model to merely acknowledge completion
        # adds tokens but no new product evidence; browser evaluation is the
        # authoritative next step for both generation and repair commits.
        logger.info(
            "[bold green]Generator[/] reusing trace-backed commit %s; no new model call made.",
            recovered_commit[:12],
        )
        return AgentRunStats(
            cost_usd=0.0,
            duration_ms=0,
            duration_api_ms=0,
            token_usage={},
            usage={"recovery": "trace_backed_commit", "commit": recovered_commit},
            model_usage={},
        )

    sprint_run_context = SprintState.load(file_comm).required_run_context(
        sprint_num,
        owner="Generator",
    )
    user_msg = _build_generator_prompt(
        mode=mode,
        file_comm=file_comm,
        round_num=round_num,
        sprint_num=sprint_num,
        sprint_context=sprint_run_context.sprint_context,
        accepted_sprints=sprint_run_context.accepted_sprints,
        resume_uncommitted_work=resume_uncommitted_work,
        recovered_commit=recovered_commit,
    )
    target_profile = file_comm.read_target_profile()
    prior_grades = file_comm.read_grades(round_num - 1) if mode == "repair" else None
    scope_contract_only = isinstance(prior_grades, dict) and _is_scope_contract_only_repair(prior_grades)
    mutation_policy = (
        MinimalPathPolicy.load(workdir, round_num)
        if config.minimal_path_guidance_enabled
        else None
    )

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.generator_model,
        system_prompt=GENERATOR_SYSTEM_PROMPT,
        max_turns=config.generator_max_turns,
        allow_bash=True,
        stop_hooks=[_make_generator_stop_hook(
            frontend_dir, baseline_commit, mode, workdir, round_num, target_profile,
            scope_contract_only=scope_contract_only,
        )],
        trace_path=RoundArtifacts(file_comm, round_num).trace_path("generator"),
        mutation_policy=mutation_policy,
    )

    _validate_generator_outputs(file_comm, workdir, (result.result or "").strip())

    if permission_denials:
        logger.warning(
            f"[bold green]Generator[/] completed with permission denials: {permission_denials}"
        )

    logger.info(
        f"[bold green]Generator[/] mode={mode} round={round_num} sprint={sprint_num} "
        f"done. Cost: ${cost:.4f}"
    )
    return build_agent_run_stats(result, model=config.generator_model)
