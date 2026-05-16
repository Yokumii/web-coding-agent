from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Literal

from src.agents.sdk_runner import (
    AgentRunStats,
    build_agent_run_stats,
    make_repair_completion_hook,
    run_sdk_agent,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.utils.logger import get_logger

logger = get_logger(__name__)

GeneratorMode = Literal["generate", "repair"]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_CLAUDE_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"
_GENERATE_REQUIRED_READS = (
    ".harness/sprint_plan.json",
    ".harness/feature_list.json",
    ".harness/design_tokens.json",
    ".harness/accepted_sprints.json",
)
_REPAIR_REQUIRED_READS = (
    ".harness/feedback_round_{feedback_round}.md",
    ".harness/grade_round_{feedback_round}.json",
    ".harness/repair_targets_round_{round_num}.json",
    ".harness/sprint_plan.json",
    ".harness/design_tokens.json",
    ".harness/accepted_sprints.json",
)

# Recognise file paths inside agent prose. We deliberately keep this loose:
# any "frontend/..." token with a recognised source extension is captured
# as a file_hint. False positives here are harmless (the agent uses them
# as suggestions); false negatives let the model dodge difficult fixes.
_FILE_HINT_RE = re.compile(
    r"frontend/[A-Za-z0-9_./\-]+\.(?:jsx?|tsx?|css|scss|html|json|svg)",
)


def _ensure_local_claude_skills(workdir: Path) -> None:
    """Expose repository-local Claude skills inside the generator workdir."""
    if not _LOCAL_CLAUDE_SKILLS_DIR.is_dir():
        return

    claude_dir = workdir / ".claude"
    skills_dir = claude_dir / "skills"
    if skills_dir.exists() or skills_dir.is_symlink():
        return

    claude_dir.mkdir(parents=True, exist_ok=True)
    try:
        skills_dir.symlink_to(_LOCAL_CLAUDE_SKILLS_DIR, target_is_directory=True)
    except OSError:
        shutil.copytree(_LOCAL_CLAUDE_SKILLS_DIR, skills_dir)


def _extract_repair_targets(
    grades: dict[str, Any],
    sprint_context: dict,
) -> dict[str, list[dict[str, Any]] | list[str]]:
    sprint_feature_ids = {
        str(feature_id).strip()
        for feature_id in sprint_context.get("feature_ids", [])
        if feature_id
    }
    failed_checks: list[dict[str, Any]] = []
    failed_criteria: list[dict[str, Any]] = []
    affected_feature_ids: set[str] = set()

    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict):
            continue
        feature_id = str(check.get("feature_id", "")).strip()
        status = str(check.get("status", "")).strip().lower()
        critical = check.get("critical") is True
        if status == "fail" or (status == "partial" and critical):
            if not feature_id or feature_id in sprint_feature_ids:
                failed_checks.append(check)
                if feature_id:
                    affected_feature_ids.add(feature_id)

    for criterion in grades.get("target_exit_criteria_results", []):
        if not isinstance(criterion, dict):
            continue
        feature_id = str(criterion.get("feature_id", "")).strip()
        if criterion.get("passed") is False:
            if not feature_id or feature_id in sprint_feature_ids:
                failed_criteria.append(criterion)
                if feature_id:
                    affected_feature_ids.add(feature_id)

    if not affected_feature_ids:
        affected_feature_ids = set(sprint_feature_ids)

    return {
        "failed_checks": failed_checks,
        "failed_criteria": failed_criteria,
        "affected_feature_ids": sorted(affected_feature_ids),
    }


def _format_failed_checks(failed_checks: list[dict[str, Any]]) -> str:
    if not failed_checks:
        return "- No structured failed UI checks were recorded."
    return "\n".join(
        (
            f"- {check.get('check_id', check.get('id', 'unknown'))} "
            f"| feature_id={check.get('feature_id', 'unknown')} "
            f"| critical={check.get('critical', False)} "
            f"| status={check.get('status', 'unknown')} "
            f"| task={check.get('task', '')}"
        )
        for check in failed_checks
    )


def _format_failed_criteria(failed_criteria: list[dict[str, Any]]) -> str:
    if not failed_criteria:
        return "- No structured failed exit criteria were recorded."
    return "\n".join(
        (
            f"- {criterion.get('criterion_id', 'unknown')} "
            f"| feature_id={criterion.get('feature_id', 'unknown')} "
            f"| critical={criterion.get('critical', False)} "
            f"| criterion={criterion.get('criterion', '')}"
        )
        for criterion in failed_criteria
    )


def _extract_file_hints(*texts: Any) -> list[str]:
    """Pull `frontend/...` source paths out of evaluator notes.

    Accepts arbitrary string-or-None inputs and returns a sorted, de-duplicated
    list of capture strings. Used to seed `file_hints` on a repair target so
    the agent has a concrete starting place and the completion hook has
    something to compare ``files_modified`` against.
    """
    hits: set[str] = set()
    for text in texts:
        if isinstance(text, str) and text:
            for match in _FILE_HINT_RE.findall(text):
                hits.add(match)
    return sorted(hits)


def build_repair_targets_payload(
    *,
    grades: dict[str, Any],
    sprint_num: int,
    round_num: int,
    sprint_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the structured target list the generator must address in repair.

    Aggregates failures from four sources in the grade JSON:

    * ``ui_checks`` with status in {fail, partial} (partial only counted when critical)
    * ``target_exit_criteria_results`` with passed=False
    * ``bugs_found`` with severity in {critical, major}
    * Free-form ``repair_instructions`` (kept verbatim as low-priority targets)

    When ``sprint_context`` is provided, ui_checks and exit_criteria are scoped
    to the sprint's feature_ids (matching :func:`_extract_repair_targets`),
    so a failure attributed to a feature in a future sprint is not pinned to
    this round's repair list.

    File hints are extracted from each item's notes/details so the generator
    has a concrete starting place. This payload is what the harness writes
    to ``.harness/repair_targets_round_N.json``; the generator must produce a
    matching ``.harness/repair_report_round_N.json`` before its Stop is allowed.
    """
    sprint_feature_ids: set[str] = set()
    if sprint_context:
        for fid in sprint_context.get("feature_ids", []) or []:
            text = str(fid).strip()
            if text:
                sprint_feature_ids.add(text)

    def _in_scope(feature_id: str) -> bool:
        if not sprint_feature_ids:
            return True
        if not feature_id:
            return True
        return feature_id in sprint_feature_ids

    targets: list[dict[str, Any]] = []

    for check in grades.get("ui_checks", []) or []:
        if not isinstance(check, dict):
            continue
        feature_id = str(check.get("feature_id", "")).strip()
        if not _in_scope(feature_id):
            continue
        status = str(check.get("status", "")).strip().lower()
        critical = check.get("critical") is True
        if status == "fail" or (status == "partial" and critical):
            check_id = str(check.get("check_id") or check.get("id") or "").strip()
            if not check_id:
                continue
            targets.append(
                {
                    "id": check_id,
                    "kind": "ui_check",
                    "critical": critical,
                    "summary": str(check.get("task", "")).strip(),
                    "details": str(check.get("notes", "")).strip(),
                    "file_hints": _extract_file_hints(check.get("notes"), check.get("task")),
                }
            )

    for criterion in grades.get("target_exit_criteria_results", []) or []:
        if not isinstance(criterion, dict):
            continue
        feature_id = str(criterion.get("feature_id", "")).strip()
        if not _in_scope(feature_id):
            continue
        if criterion.get("passed") is False:
            criterion_id = str(criterion.get("criterion_id", "")).strip()
            if not criterion_id:
                continue
            targets.append(
                {
                    "id": criterion_id,
                    "kind": "exit_criterion",
                    "critical": criterion.get("critical") is True,
                    "summary": str(criterion.get("criterion", "")).strip(),
                    "details": str(criterion.get("notes", "")).strip(),
                    "file_hints": _extract_file_hints(
                        criterion.get("notes"), criterion.get("criterion")
                    ),
                }
            )

    for bug in grades.get("bugs_found", []) or []:
        if not isinstance(bug, dict):
            continue
        severity = str(bug.get("severity", "")).strip().lower()
        if severity not in {"critical", "major", "high"}:
            continue
        bug_id = str(bug.get("id") or bug.get("bug_id") or "").strip()
        if not bug_id:
            continue
        file_hints = _extract_file_hints(bug.get("summary"), bug.get("notes"))
        if isinstance(bug.get("file"), str):
            file_hints = sorted(set(file_hints) | {bug["file"]})
        targets.append(
            {
                "id": bug_id,
                "kind": "bug",
                "critical": severity == "critical",
                "summary": str(bug.get("summary", "")).strip(),
                "details": str(bug.get("notes", "")).strip(),
                "file_hints": file_hints,
            }
        )

    for index, instruction in enumerate(grades.get("repair_instructions", []) or [], start=1):
        if not isinstance(instruction, str) or not instruction.strip():
            continue
        targets.append(
            {
                "id": f"REPAIR-{index:02d}",
                "kind": "repair_instruction",
                "critical": False,
                "summary": instruction.strip()[:200],
                "details": instruction.strip(),
                "file_hints": _extract_file_hints(instruction),
            }
        )

    return {
        "round": round_num,
        "sprint": sprint_num,
        "targets": targets,
    }


def _get_sprint_context(file_comm: FileComm, sprint_num: int) -> dict:
    sprint_plan = file_comm.read_sprint_plan()
    if sprint_plan is None:
        raise RuntimeError("Generator requires .harness/sprint_plan.json, but it was not found.")

    sprints = sprint_plan.get("sprints")
    if not isinstance(sprints, list):
        raise RuntimeError("Generator found invalid .harness/sprint_plan.json: sprints must be an array.")

    for sprint in sprints:
        if isinstance(sprint, dict) and sprint.get("number") == sprint_num:
            return sprint

    raise RuntimeError(f"Generator could not find sprint {sprint_num} in .harness/sprint_plan.json.")


def _get_accepted_sprints(file_comm: FileComm) -> dict:
    accepted_sprints = file_comm.read_accepted_sprints()
    if accepted_sprints is None:
        raise RuntimeError(
            "Generator requires .harness/accepted_sprints.json, but it was not found."
        )
    return accepted_sprints


def _build_generate_prompt(
    *,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    sprint_context: dict,
    accepted_sprints: dict,
) -> str:
    accepted = accepted_sprints.get("accepted", [])
    required_reads = list(_GENERATE_REQUIRED_READS)
    previous_round = round_num - 1
    if previous_round >= 1:
        previous_feedback = file_comm.dir / f"feedback_round_{previous_round}.md"
        previous_grades = file_comm.dir / f"grade_round_{previous_round}.json"
        if previous_feedback.exists():
            required_reads.append(f".harness/{previous_feedback.name}")
        if previous_grades.exists():
            required_reads.append(f".harness/{previous_grades.name}")
    required_reads_text = "\n".join(f"- {path}" for path in required_reads)
    feature_ids = ", ".join(sprint_context.get("feature_ids", []))
    deliverables = "\n".join(f"- {item}" for item in sprint_context.get("deliverables", []))
    exit_criteria = "\n".join(f"- {item}" for item in sprint_context.get("exit_criteria", []))
    return (
        f"Mode: generate\n"
        f"Round: {round_num}\n"
        f"Sprint: {sprint_num}\n"
        f"Sprint Title: {sprint_context.get('title')}\n"
        f"Sprint Goal: {sprint_context.get('goal')}\n"
        f"Target Feature IDs: {feature_ids}\n"
        f"Deliverables:\n{deliverables}\n"
        f"Exit Criteria:\n{exit_criteria}\n"
        f"Accepted Sprints: {accepted}\n"
        f"Required Reads:\n{required_reads_text}\n\n"
        f"Implement only sprint {sprint_num}.\n"
        f"Set up or update the frontend-only project in `frontend/`.\n"
        f"Do not implement future sprint functionality or unrelated refactors.\n"
        f"If previous-round feedback or grades are present, read them to preserve accepted work, "
        f"avoid regressions, and carry forward non-blocking polish notes without re-opening already accepted sprint scope.\n"
        f"If `.claude/skills/ui-ux-pro-max/SKILL.md` exists in the workdir, consult and use it for UI/UX design and review decisions.\n"
        f"Use paths relative to the workdir when calling tools; do not use absolute paths.\n"
        f"For Bash, use exactly one command per tool call with no shell control operators or redirection.\n"
        f"Allowed examples: `ls frontend`, `npm create vite@latest frontend -- --template react`, "
        f"`npm install --prefix frontend`, `head -20 frontend/package.json`.\n"
        f"When done, update `.harness/build_log.md` with round, sprint, mode, implemented features, "
        f"and a short summary of what was completed.\n"
        f"Also append a short progress entry to `.harness/progress.md`.\n"
        f"Treat `.` as the workdir root."
    )


def _build_repair_prompt(
    *,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    sprint_context: dict,
    accepted_sprints: dict,
) -> str:
    feedback_round = round_num - 1
    accepted = accepted_sprints.get("accepted", [])
    previous_grades = file_comm.read_grades(feedback_round)
    if previous_grades is None:
        # Without the previous grade JSON the repair prompt has no failed
        # checks / criteria to reference and would degrade into a no-direction
        # generate. Fail loudly so the harness can decide whether to fall
        # back to mode=generate or abort.
        raise RuntimeError(
            f"Generator repair mode requires .harness/grade_round_{feedback_round}.json "
            f"from the previous round, but it was not found. The previous round may "
            f"have crashed before writing grades."
        )
    repair_targets = _extract_repair_targets(previous_grades, sprint_context)
    # Write the structured targets file the Stop hook will enforce against.
    targets_payload = build_repair_targets_payload(
        grades=previous_grades,
        sprint_num=sprint_num,
        round_num=round_num,
        sprint_context=sprint_context,
    )
    file_comm.write_repair_targets(round_num, targets_payload)
    required_reads = "\n".join(
        f"- {path.format(feedback_round=feedback_round, round_num=round_num)}"
        for path in _REPAIR_REQUIRED_READS
    )
    feature_ids = ", ".join(sprint_context.get("feature_ids", []))
    affected_feature_ids = ", ".join(repair_targets["affected_feature_ids"]) or "None declared"
    failed_criteria = _format_failed_criteria(repair_targets["failed_criteria"])
    failed_checks = _format_failed_checks(repair_targets["failed_checks"])
    target_id_list = ", ".join(t["id"] for t in targets_payload["targets"]) or "(none)"
    return (
        f"Mode: repair\n"
        f"Round: {round_num}\n"
        f"Sprint: {sprint_num}\n"
        f"Sprint Title: {sprint_context.get('title')}\n"
        f"Repair Scope: Fix evaluator-reported issues for the current sprint only\n"
        f"Target Feature IDs: {feature_ids}\n"
        f"Affected Feature IDs: {affected_feature_ids}\n"
        f"Accepted Sprints: {accepted}\n"
        f"Failed Exit Criteria:\n{failed_criteria}\n"
        f"Failed UI Checks:\n{failed_checks}\n"
        f"Required Reads:\n{required_reads}\n\n"
        f"## Repair Completion Protocol\n"
        f"Before you may end your turn:\n"
        f"1. Read .harness/repair_targets_round_{round_num}.json. Each entry under "
        f'"targets" must be addressed. Targets in this round: {target_id_list}.\n'
        f"2. Implement fixes for every target. If a target is genuinely unfixable in "
        f"this sprint, document the reason rather than skipping silently.\n"
        f"3. Write .harness/repair_report_round_{round_num}.json with one entry per "
        f"target listing:\n"
        f"   - target_id (verbatim from the targets file)\n"
        f"   - addressed (true | false)\n"
        f"   - files_modified (list of frontend/* paths you actually edited)\n"
        f"   - notes (1-2 sentences) or reason (when addressed=false)\n"
        f"4. Only after the report is written may you stop. The harness will block "
        f"your stop attempt and feed back missing items if the report is incomplete.\n\n"
        f"Fix ONLY the issues needed for sprint acceptance or regression recovery.\n"
        f"Do not implement new features from future sprints.\n"
        f"Do not start work for the next sprint.\n"
        f"If `.claude/skills/ui-ux-pro-max/SKILL.md` exists in the workdir, consult and use it for UI/UX design and review decisions.\n"
        f"Use paths relative to the workdir when calling tools; do not use absolute paths.\n"
        f"For Bash, use exactly one command per tool call with no shell control operators or redirection.\n"
        f"Allowed examples: `ls frontend/src`, `grep -n \"pattern\" frontend/src/App.jsx`, "
        f"`npm install --prefix frontend`, `head -20 .harness/grade_round_{feedback_round}.json`.\n"
        f"When done, update `.harness/build_log.md` with round, sprint, mode, addressed issues, "
        f"and a short summary of what was repaired.\n"
        f"Also append a short progress entry to `.harness/progress.md`.\n"
        f"Treat `.` as the workdir root."
    )


def _validate_generator_outputs(file_comm: FileComm, workdir: Path, result_summary: str) -> None:
    frontend_dir = workdir / "frontend"
    package_json = frontend_dir / "package.json"

    if frontend_dir.exists() and package_json.exists():
        return

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
    """Run generator agent. Returns execution stats."""
    logger.info(
        f"[bold green]Generator[/] starting mode={mode} round={round_num} sprint={sprint_num}"
    )
    _ensure_local_claude_skills(workdir)

    sprint_context = _get_sprint_context(file_comm, sprint_num)
    accepted_sprints = _get_accepted_sprints(file_comm)

    if mode == "generate":
        user_msg = _build_generate_prompt(
            file_comm=file_comm,
            workdir=workdir,
            round_num=round_num,
            sprint_num=sprint_num,
            sprint_context=sprint_context,
            accepted_sprints=accepted_sprints,
        )
        stop_hook = None
    else:
        user_msg = _build_repair_prompt(
            file_comm=file_comm,
            workdir=workdir,
            round_num=round_num,
            sprint_num=sprint_num,
            sprint_context=sprint_context,
            accepted_sprints=accepted_sprints,
        )
        stop_hook = make_repair_completion_hook(
            targets_path=file_comm.dir / f"repair_targets_round_{round_num}.json",
            report_path=file_comm.dir / f"repair_report_round_{round_num}.json",
            max_block_attempts=config.max_repair_block_attempts,
            file_comm=file_comm,
            round_num=round_num,
        )

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.generator_model,
        system_prompt=GENERATOR_SYSTEM_PROMPT,
        max_turns=config.generator_max_turns,
        allow_bash=True,
        stop_hook=stop_hook,
        trace_path=file_comm.dir / "traces" / f"generator_round_{round_num}.jsonl",
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
