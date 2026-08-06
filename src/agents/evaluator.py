from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents._shared import expose_local_claude_skills
from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.design_contract import DesignContractContext
from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts
from src.orchestration.sprint_state import SprintRunContext, SprintState
from src.orchestration.target_profile import target_profile_guidance
from src.prompts.evaluator import EVALUATOR_SYSTEM_PROMPT
from src.prompts.grading import determine_passed as _determine_passed
from src.utils.llm_json import extract_json_object
from src.utils.logger import get_logger

logger = get_logger(__name__)

_EVALUATOR_REQUIRED_READS = [
    ".harness/spec.md",
    ".harness/design_tokens.json",
    ".harness/feature_list.json",
    ".harness/sprint_plan.json",
    ".harness/ui_verification_plan.json",
    ".harness/accepted_sprints.json",
]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_CLAUDE_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"


def _make_evaluator_stop_hook(file_comm: FileComm, round_num: int):
    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        missing = []
        if not file_comm.read_grades(round_num):
            missing.append("the round grades JSON")
        if not file_comm.read_visual_manifest(round_num):
            missing.append("the visual screenshot manifest")
        if missing:
            reason = "Evaluator must write " + " and ".join(missing) + " before finishing."
            return {"decision": "block", "reason": reason, "stopReason": reason}
        return {"continue_": True}
    return _hook


def _ensure_local_claude_skills(workdir: Path) -> None:
    """将仓库内置 skills 暴露到 evaluator 的工作目录。"""
    expose_local_claude_skills(workdir, _LOCAL_CLAUDE_SKILLS_DIR)


def _build_visual_capture_requirements(*, artifacts: RoundArtifacts, app_url: str) -> list[str]:
    """构造 Phase C 所需的截图与 manifest 写入要求。"""
    home_ref, mid_ref, bottom_ref = artifacts.visual_capture_refs
    return [
        "Phase C: Deferred Visual Review Capture",
        f"- Capture `{home_ref}` with browser_screenshot position=`top`.",
        f"- If the page meaningfully scrolls, capture `{mid_ref}` with position=`middle`.",
        f"- If the page meaningfully scrolls, capture `{bottom_ref}` with position=`bottom`.",
        f"- Write `{artifacts.visual_manifest_ref}` with this schema:",
        json.dumps(
            {
                "round": artifacts.round_num,
                "app_url": app_url,
                "screenshots": artifacts.visual_capture_refs,
                "notes": "short paragraph describing what was captured",
            },
            indent=2,
        ),
        "- Only include screenshots that were actually created.",
        "- Use only relative paths such as `.harness/visual_round_1_home.png`.",
        "- Save screenshots via the browser screenshot tool filename argument.",
        "- Write the manifest with the Write tool only.",
        "- Keep the appearance verdict as a placeholder for the downstream VLM review; do not treat this capture step as the final visual score.",
    ]


async def run_evaluator(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    app_url: str,
    edit_guard: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], AgentRunStats]:
    """运行 evaluator，并返回通过状态、评分结果与执行统计。"""
    if config.evaluator_mode == "simple":
        from src.agents.simple_evaluator import run_simple_evaluator
        sprint_num = SprintState.load(file_comm).current_run_context().sprint_num
        return await run_simple_evaluator(
            file_comm=file_comm, workdir=workdir, round_num=round_num,
            sprint_num=sprint_num, app_url=app_url,
        )
    if config.evaluator_mode != "full":
        raise ValueError(f"unsupported EVALUATOR_MODE: {config.evaluator_mode!r}")
    _ensure_local_claude_skills(workdir)
    sprint_run_context = SprintState.load(file_comm).current_run_context()
    sprint_num = sprint_run_context.sprint_num

    logger.info(
        f"[bold yellow]Evaluator[/] round {round_num} starting at {app_url} "
        f"for sprint {sprint_num}"
    )

    user_msg = _build_evaluator_prompt(
        file_comm=file_comm,
        workdir=workdir,
        round_num=round_num,
        sprint_num=sprint_num,
        sprint_run_context=sprint_run_context,
        app_url=app_url,
        edit_guard=edit_guard,
    )
    response, total_cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.evaluator_model,
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        max_turns=config.evaluator_max_turns,
        allow_bash=False,
        bash_profile="read_only",
        allow_playwright=True,
        stop_hooks=[_make_evaluator_stop_hook(file_comm, round_num)],
        trace_path=RoundArtifacts(file_comm, round_num).trace_path("evaluator"),
    )

    grades = file_comm.read_grades(round_num)
    if not grades:
        grades = _extract_grades_from_response(response)

    if permission_denials:
        logger.warning(
            f"[bold yellow]Evaluator[/] completed with permission denials: {permission_denials}"
        )
    logger.info(
        f"[bold yellow]Evaluator[/] round {round_num} completed raw evaluation. "
        f"Cost: ${total_cost:.4f}"
    )

    return _determine_passed(grades), grades or {}, build_agent_run_stats(
        response, model=config.evaluator_model
    )


def _build_evaluator_prompt(
    *,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    sprint_run_context: SprintRunContext,
    app_url: str,
    edit_guard: dict[str, Any] | None = None,
) -> str:
    round_artifacts = RoundArtifacts(file_comm, round_num)
    design_contract = DesignContractContext.load(file_comm)
    required_reads = list(_EVALUATOR_REQUIRED_READS)
    target_profile = file_comm.read_target_profile()
    if target_profile:
        required_reads.append(".harness/target_profile.json")
    required_reads.extend(design_contract.required_refs())
    required_reads.extend(round_artifacts.previous_existing_refs())
    if edit_guard is not None:
        required_reads.extend([
            ".harness/edit_dom_baseline.json",
            f".harness/edit_scope_round_{round_num}.json",
        ])

    sprint_context = sprint_run_context.sprint_context
    accepted_sprints = sprint_run_context.accepted_sprints
    feature_ids = sprint_context.get("feature_ids", [])
    deliverables = sprint_context.get("deliverables", [])
    exit_criteria = sprint_context.get("exit_criteria", [])
    ui_checks = sprint_run_context.ui_checks
    exit_criterion_map = sprint_run_context.exit_criterion_map

    lines = [
        f"Application URL: {app_url}",
        f"Round: {round_num}",
        f"Sprint: {sprint_num}",
        f"Sprint Title: {sprint_context.get('title', 'Unknown Sprint')}",
        f"Sprint Goal: {sprint_context.get('goal', 'Validate the current sprint.')}",
        f"Target Feature IDs: {', '.join(feature_ids) if feature_ids else 'None declared'}",
        f"Accepted Sprints: {', '.join(str(item) for item in accepted_sprints.get('accepted', [])) or 'None'}",
        "",
        "Sprint Deliverables:",
        *(
            [f"- {item}" for item in deliverables]
            if deliverables
            else ["- No explicit deliverables declared."]
        ),
        "",
        "Sprint Exit Criteria:",
        *(
            [f"- {item}" for item in exit_criteria]
            if exit_criteria
            else ["- No explicit exit criteria declared."]
        ),
        "",
        "Exit Criterion Feature Mapping:",
        *(
            [
                (
                    f"- criterion_id={item.get('criterion_id', 'unknown')} "
                    f"| feature_id={item.get('feature_id', 'unknown')} "
                    f"| critical={item.get('critical', True)} "
                    f"| criterion={item.get('criterion', '')}"
                )
                for item in exit_criterion_map
            ]
            if exit_criterion_map
            else ["- No explicit exit criterion mapping could be derived."]
        ),
        "",
        "Current Sprint UI Verification Checks:",
        *(
            [
                (
                    f"- check_id={check.get('id', 'unknown')} | feature_id={check.get('feature_id', 'unknown')} "
                    f"| critical={check.get('critical', False)} | task={check.get('task', '')} "
                    f"| expected={check.get('expected_result', '')}"
                )
                for check in ui_checks
            ]
            if ui_checks
            else ["- No explicit UI checks declared for this sprint."]
        ),
        "",
        "Required Reads:",
        *[f"- {path}" for path in required_reads],
        "",
        "Assessment Order:",
        "1. Phase A: Render Gate",
        "2. Phase B: UI Functionality Verification",
        "3. Phase C: Deferred Visual Review Capture",
        "4. Phase D: Source Inspection",
        "5. Phase E: Score Aggregation And Verdict",
        "",
        *(
            [
                "Edit Scope Contract (independent audit required):",
                "- The generator declared a narrow editable surface set. Verify it is genuinely necessary for this sprint goal; it must not be a catch-all for unrelated accepted functionality.",
                "- Treat an invalid semantic contract as a regression even if the requested feature works.",
                "- Write `edit_scope_audit` as `pass` only when the declared scope is proportionate to the sprint; otherwise write `fail` and explain it in regressions_found.",
                "- Machine guard result:",
                json.dumps(edit_guard, ensure_ascii=False),
                "",
            ]
            if edit_guard is not None else []
        ),
        target_profile_guidance(target_profile).strip(),
        (
            "During source inspection, verify that frontend/submission contains the requested "
            "target-platform source and that it matches the browser preview."
            if target_profile and target_profile.get("profile") != "web"
            else ""
        ),
        "",
        "Output Files:",
        f"1. {round_artifacts.feedback_ref}",
        f"2. {round_artifacts.grade_ref}",
        f"3. {round_artifacts.visual_manifest_ref}",
        "",
        *_build_visual_capture_requirements(artifacts=round_artifacts, app_url=app_url),
        "",
        *design_contract.evaluator_assessment_lines(),
        "If `.claude/skills/webapp-testing/SKILL.md` exists in the workdir, consult and use it for browser testing and evaluation strategy.",
        "Use paths relative to the workdir when calling file tools; do not use absolute paths.",
        "Treat `.` as the workdir root.",
    ]
    return "\n".join(lines)


_GRADE_LIKE_KEYS = ("criteria", "phase_results", "round")


def _iter_candidate_objects(text: str) -> list[dict[str, Any]]:
    """扫描文本中的多个 JSON 对象，供评分提取逻辑逐个筛选。"""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        next_brace = text.find("{", cursor)
        if next_brace < 0:
            break
        try:
            obj, consumed = decoder.raw_decode(text[next_brace:])
        except json.JSONDecodeError:
            cursor = next_brace + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        cursor = next_brace + consumed
    return objects


def _extract_grades_from_response(response) -> dict[str, Any] | None:
    """从 evaluator 的文本回复中提取最像评分结果的那份 JSON。"""
    texts: list[str] = []

    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", "text")
        if block_type != "text":
            continue
        text = getattr(block, "text", "")
        if text:
            texts.append(text)

    result_text = getattr(response, "result", None)
    if isinstance(result_text, str) and result_text:
        texts.append(result_text)

    # 优先检查 message.content，再检查 result；同一段文本内优先取最后
    # 一个“像评分结果”的对象，兼容先给草稿再给最终版的输出习惯。
    for text in texts:
        if "{" not in text:
            continue
        try:
            direct = extract_json_object(text)
        except ValueError:
            direct = None
        if isinstance(direct, dict) and any(key in direct for key in _GRADE_LIKE_KEYS):
            return direct
        candidates = _iter_candidate_objects(text)
        for candidate in reversed(candidates):
            if any(key in candidate for key in _GRADE_LIKE_KEYS):
                return candidate
    return None
