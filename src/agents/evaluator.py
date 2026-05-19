from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents._shared import expose_local_claude_skills
from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.evaluator import EVALUATOR_SYSTEM_PROMPT
from src.prompts.grading import check_grades
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


def _ensure_local_claude_skills(workdir: Path) -> None:
    """将仓库内置 skills 暴露到 evaluator 的工作目录。"""
    expose_local_claude_skills(workdir, _LOCAL_CLAUDE_SKILLS_DIR)


def _build_visual_capture_requirements(*, round_num: int, app_url: str) -> list[str]:
    """构造 Phase C 所需的截图与 manifest 写入要求。"""
    return [
        "Phase C: Deferred Visual Review Capture",
        f"- During this evaluator run, capture `.harness/visual_round_{round_num}_home.png` at the top of the page.",
        f"- If the page meaningfully scrolls, also capture `.harness/visual_round_{round_num}_mid.png` from a middle section.",
        f"- If the page meaningfully scrolls, also capture `.harness/visual_round_{round_num}_bottom.png` near the bottom section.",
        f"- Write `.harness/visual_manifest_round_{round_num}.json` with this schema:",
        json.dumps(
            {
                "round": round_num,
                "app_url": app_url,
                "screenshots": [
                    f".harness/visual_round_{round_num}_home.png",
                    f".harness/visual_round_{round_num}_mid.png",
                    f".harness/visual_round_{round_num}_bottom.png",
                ],
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
) -> tuple[bool, dict[str, Any], AgentRunStats]:
    """运行 evaluator，并返回通过状态、评分结果与执行统计。"""
    _ensure_local_claude_skills(workdir)
    sprint_num, sprint_context = _get_current_sprint_context(file_comm)
    accepted_sprints = _get_accepted_sprints(file_comm)

    logger.info(
        f"[bold yellow]Evaluator[/] round {round_num} starting at {app_url} "
        f"for sprint {sprint_num}"
    )

    user_msg = _build_evaluator_prompt(
        file_comm=file_comm,
        workdir=workdir,
        round_num=round_num,
        sprint_num=sprint_num,
        sprint_context=sprint_context,
        accepted_sprints=accepted_sprints,
        app_url=app_url,
    )
    response, total_cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.evaluator_model,
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        max_turns=config.evaluator_max_turns,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=True,
        trace_path=file_comm.dir / "traces" / f"evaluator_round_{round_num}.jsonl",
    )

    grades = file_comm.read_grades(round_num)
    if not grades:
        grades = _extract_grades_from_response(response)

    passed = _determine_passed(grades)
    status = "[bold green]PASSED[/]" if passed else "[bold red]FAILED[/]"
    if permission_denials:
        logger.warning(
            f"[bold yellow]Evaluator[/] completed with permission denials: {permission_denials}"
        )
    logger.info(f"[bold yellow]Evaluator[/] round {round_num} {status}. Cost: ${total_cost:.4f}")

    return passed, grades or {}, build_agent_run_stats(response, model=config.evaluator_model)


def _get_current_sprint_context(file_comm: FileComm) -> tuple[int, dict[str, Any]]:
    accepted_sprints = _get_accepted_sprints(file_comm)
    sprint_num = int(accepted_sprints.get("current_target", 1))
    sprint_plan = file_comm.read_sprint_plan() or {}
    for sprint in sprint_plan.get("sprints", []):
        if sprint.get("number") == sprint_num:
            return sprint_num, sprint
    return sprint_num, {}


def _get_accepted_sprints(file_comm: FileComm) -> dict[str, Any]:
    return file_comm.read_accepted_sprints() or {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


def _get_current_sprint_ui_checks(file_comm: FileComm, sprint_num: int) -> list[dict[str, Any]]:
    verification_plan = file_comm.read_ui_verification_plan() or {}
    for sprint in verification_plan.get("sprints", []):
        if sprint.get("sprint") == sprint_num:
            checks = sprint.get("checks", [])
            return [check for check in checks if isinstance(check, dict)]
    return []


def _get_current_sprint_features(file_comm: FileComm, sprint_num: int) -> list[dict[str, Any]]:
    feature_list = file_comm.read_feature_list() or {}
    features = feature_list.get("features", [])
    return [
        feature
        for feature in features
        if isinstance(feature, dict) and feature.get("sprint") == sprint_num
    ]


def _build_exit_criterion_feature_map(
    file_comm: FileComm,
    sprint_num: int,
    sprint_context: dict[str, Any],
) -> list[dict[str, Any]]:
    exit_criteria = sprint_context.get("exit_criteria", [])
    sprint_features = _get_current_sprint_features(file_comm, sprint_num)
    feature_ids = [str(feature.get("id")) for feature in sprint_features if feature.get("id")]
    single_feature_id = feature_ids[0] if len(feature_ids) == 1 else ""

    mappings: list[dict[str, Any]] = []
    for index, criterion in enumerate(exit_criteria, start=1):
        criterion_text = str(criterion).strip()
        if not criterion_text:
            continue

        matched_feature_id = ""
        for feature in sprint_features:
            acceptance = feature.get("acceptance_criteria", [])
            if criterion_text in [str(item).strip() for item in acceptance]:
                matched_feature_id = str(feature.get("id", "")).strip()
                break

        if not matched_feature_id and single_feature_id:
            matched_feature_id = single_feature_id

        mappings.append(
            {
                "criterion_id": f"EXIT-{sprint_num:02d}-{index:02d}",
                "feature_id": matched_feature_id or "unknown",
                "criterion": criterion_text,
                "critical": True,
            }
        )

    return mappings


def _build_evaluator_prompt(
    *,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    sprint_context: dict[str, Any],
    accepted_sprints: dict[str, Any],
    app_url: str,
) -> str:
    previous_round = round_num - 1
    required_reads = list(_EVALUATOR_REQUIRED_READS)
    if previous_round >= 1:
        previous_feedback = file_comm.dir / f"feedback_round_{previous_round}.md"
        previous_grades = file_comm.dir / f"grade_round_{previous_round}.json"
        if previous_feedback.exists():
            required_reads.append(f".harness/{previous_feedback.name}")
        if previous_grades.exists():
            required_reads.append(f".harness/{previous_grades.name}")

    feature_ids = sprint_context.get("feature_ids", [])
    deliverables = sprint_context.get("deliverables", [])
    exit_criteria = sprint_context.get("exit_criteria", [])
    ui_checks = _get_current_sprint_ui_checks(file_comm, sprint_num)
    exit_criterion_map = _build_exit_criterion_feature_map(file_comm, sprint_num, sprint_context)

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
        "Output Files:",
        f"1. .harness/feedback_round_{round_num}.md",
        f"2. .harness/grade_round_{round_num}.json",
        f"3. .harness/visual_manifest_round_{round_num}.json",
        "",
        *_build_visual_capture_requirements(round_num=round_num, app_url=app_url),
        "",
        "If `.claude/skills/webapp-testing/SKILL.md` exists in the workdir, consult and use it for browser testing and evaluation strategy.",
        "Use paths relative to the workdir when calling file tools; do not use absolute paths.",
        "Treat `.` as the workdir root.",
    ]
    return "\n".join(lines)


def _determine_passed(grades: dict[str, Any] | None) -> bool:
    """按关键字段与评分阈值综合判断当前轮是否通过。"""
    if not grades:
        return False

    if _parse_tristate(grades.get("sprint_passed")) is False:
        return False

    if _has_failed_critical_ui_checks(grades):
        return False

    if _has_failed_critical_exit_criteria(grades):
        return False

    overall_passed = _parse_tristate(grades.get("overall_passed"))
    if overall_passed is not None:
        return overall_passed and check_grades(grades)

    return check_grades(grades)


_TRUTHY_STRINGS = frozenset({"true", "yes", "1", "y", "t", "pass", "passed", "ok"})
_FALSEY_STRINGS = frozenset({"false", "no", "0", "n", "f", "fail", "failed"})
_FAIL_STATUSES = frozenset({"fail", "failed", "partial"})


def _parse_tristate(value: Any) -> bool | None:
    """将 agent 输出解析为 True / False / 未知 三态值。

    兼容布尔字符串与 0/1 数值；含义仍不明确时返回 ``None``。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_STRINGS:
            return True
        if normalized in _FALSEY_STRINGS:
            return False
    return None


def _has_failed_critical_ui_checks(grades: dict[str, Any]) -> bool:
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict):
            continue
        if _parse_tristate(check.get("critical")) is not True:
            continue
        status = str(check.get("status", "")).strip().lower()
        if status in _FAIL_STATUSES:
            return True
    return False


def _has_failed_critical_exit_criteria(grades: dict[str, Any]) -> bool:
    for result in grades.get("target_exit_criteria_results", []):
        if not isinstance(result, dict):
            continue
        if _parse_tristate(result.get("critical")) is not True:
            continue
        if _parse_tristate(result.get("passed")) is False:
            return True
    return False


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
