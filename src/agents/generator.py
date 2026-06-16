from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from src.agents._shared import expose_local_claude_skills
from src.agents.sdk_runner import (
    AgentRunStats,
    build_agent_run_stats,
    run_sdk_agent,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts
from src.orchestration.sprint_state import SprintState
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.prompts.grading import criterion_threshold
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
_DESIGN_REQUIRED_READS = (
    ".harness/design/design_brief.json",
    ".harness/design/layout_contract.json",
    ".harness/design/asset_manifest.json",
)


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

    return "\n".join(lines) if lines else "(no specific failures found in previous grades)"


def _design_required_reads(file_comm: FileComm) -> list[str]:
    if file_comm.read_design_brief() is None:
        return []
    return list(_DESIGN_REQUIRED_READS)


def _build_design_stage_guidance(file_comm: FileComm) -> str:
    design_brief = file_comm.read_design_brief()
    if design_brief is None:
        return ""

    visual_strategy = str(design_brief.get("visual_strategy", "")).strip()
    aesthetic_intent = design_brief.get("aesthetic_intent")
    lines = [
        "Design Stage Guidance:",
        "- Read the design-stage artifacts before making layout decisions.",
        "- Treat `layout_contract.json` as the source of semantic overlay regions, responsive behavior, and safe zones.",
        "- Treat `asset_manifest.json` as the source of which raster assets are production assets and where they should be copied.",
    ]
    if isinstance(aesthetic_intent, dict):
        hypothesis = str(aesthetic_intent.get("design_hypothesis", "")).strip()
        preserve = [
            str(item).strip()
            for item in aesthetic_intent.get("distinctive_features_to_preserve", []) or []
            if str(item).strip()
        ]
        avoid = [
            str(item).strip()
            for item in aesthetic_intent.get("generic_patterns_to_avoid", []) or []
            if str(item).strip()
        ]
        if hypothesis:
            lines.append(f"- Preserve the design hypothesis: {hypothesis}")
        if preserve:
            lines.append(
                "- Preserve the authored visual traits declared in the design brief: "
                + "; ".join(preserve)
                + "."
            )
        if avoid:
            lines.append(
                "- Do not collapse the implementation back into these generic patterns: "
                + "; ".join(avoid)
                + "."
            )
    if visual_strategy == "image_backed_ui":
        lines.extend(
            [
                "- Preserve the approved composition using the design contract and asset manifest.",
                "- Keep user-visible text and interactive controls as semantic HTML overlays.",
                "- Copy required production assets from `.harness/design/` into the frontend project before referencing them in code.",
                "- Use the image layer for composition, material, and texture; rebuild all functional labels, controls, and state in HTML.",
            ]
        )
    elif visual_strategy == "concept_reference_only":
        lines.extend(
            [
                "- Use the approved concept as a visual reference only; do not embed it as production UI.",
                "- Preserve its hierarchy and composition while rebuilding all user-visible text and controls as semantic HTML.",
                "- Do not assume a text-free background asset exists unless `asset_manifest.json` declares one.",
            ]
        )
    elif visual_strategy == "text_only_fallback":
        lines.append(
            "- The design stage fell back to text-only; continue from the planning artifacts without assuming image assets exist."
        )
    else:
        lines.append("- Follow the visual strategy declared in `design_brief.json`.")
    return "\n".join(lines)


def _build_generator_prompt(
    *,
    mode: GeneratorMode,
    file_comm: FileComm,
    round_num: int,
    sprint_num: int,
    sprint_context: dict,
    accepted_sprints: dict,
) -> str:
    """构造 generator 单轮提示词，按 generate/repair 两种模式切换细节。"""
    round_artifacts = RoundArtifacts(file_comm, round_num)
    accepted = accepted_sprints.get("accepted", [])
    feature_ids = ", ".join(sprint_context.get("feature_ids", []))
    common_lines = [
        f"Mode: {mode}\n",
        f"Round: {round_num}\n"
        f"Sprint: {sprint_num}\n"
        f"Sprint Title: {sprint_context.get('title')}\n"
        f"Target Feature IDs: {feature_ids}\n"
        f"Accepted Sprints: {accepted}\n"
    ]

    if mode == "generate":
        required_reads = list(_GENERATE_REQUIRED_READS)
        required_reads.extend(_design_required_reads(file_comm))
        required_reads.extend(round_artifacts.previous_existing_refs())
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        deliverables = "\n".join(f"- {item}" for item in sprint_context.get("deliverables", []))
        exit_criteria = "\n".join(f"- {item}" for item in sprint_context.get("exit_criteria", []))
        design_guidance = _build_design_stage_guidance(file_comm)
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        mode_lines = [
            f"Sprint Goal: {sprint_context.get('goal')}\n"
            f"Deliverables:\n{deliverables}\n"
            f"Exit Criteria:\n{exit_criteria}\n"
            f"Required Reads:\n{required_reads_text}\n\n"
            f"{design_guidance_block}"
            f"Implement only sprint {sprint_num}.\n"
            f"Set up or update the frontend-only project in `frontend/`.\n"
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
        design_guidance = _build_design_stage_guidance(file_comm)
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        required_reads = [
            previous_artifacts.feedback_ref,
            previous_artifacts.grade_ref,
            ".harness/sprint_plan.json",
            ".harness/design_tokens.json",
            ".harness/accepted_sprints.json",
            *_design_required_reads(file_comm),
        ]
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        mode_lines = [
            "Repair Scope: Fix evaluator-reported issues for the current sprint only\n"
            f"Required Reads:\n{required_reads_text}\n\n"
            f"{design_guidance_block}"
            "## Previous evaluation findings\n\n"
            f"{failures_text}\n\n"
            "## Your task\n"
            "Address every failure above. Do not stop until each one is fixed. "
            "There is no self-report file; the next evaluation round verifies your work.\n"
            "Fix ONLY the issues needed for sprint acceptance or regression recovery.\n"
            "Do not implement new features from future sprints.\n"
            "Do not start work for the next sprint.\n"
        ]
        allowed_examples = (
            f"Allowed examples: `ls frontend/src`, `grep -n \"pattern\" frontend/src/App.jsx`, "
            f"`npm install --prefix frontend`, `npm --prefix frontend run build`, "
            f"`cd frontend && npm run build`, `find frontend/src -type f | head -40`, "
            f"`head -20 {previous_artifacts.grade_ref}`.\n"
        )
        build_log_instruction = (
            "When done, update `.harness/build_log.md` with round, sprint, mode, addressed issues, "
            "and a short summary of what was repaired.\n"
        )

    common_tail = (
        "If `.claude/skills/ui-ux-pro-max/SKILL.md` exists in the workdir, consult and use it for UI/UX design and review decisions.\n"
        "Use paths relative to the workdir when calling tools; do not use absolute paths.\n"
        "For Bash, command chains and pipelines are allowed when each segment stays inside the workdir.\n"
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
    """运行 generator，并返回统一的执行统计信息。"""
    logger.info(
        f"[bold green]Generator[/] starting mode={mode} round={round_num} sprint={sprint_num}"
    )
    _ensure_local_claude_skills(workdir)

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
    )

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.generator_model,
        system_prompt=GENERATOR_SYSTEM_PROMPT,
        max_turns=config.generator_max_turns,
        allow_bash=True,
        trace_path=RoundArtifacts(file_comm, round_num).trace_path("generator"),
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
