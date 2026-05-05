from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.planner import PLANNER_SYSTEM_PROMPT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_REQUIRED_SPEC_HEADERS = (
    "## Product Overview",
    "## Target Users",
    "## Feature Descriptions",
    "## Technical Architecture",
    "## Visual Design Direction",
)
_DISALLOWED_FALLBACK_PHRASES = (
    "encountering technical issues",
    "let me provide you",
    "would you like me",
    "attempt saving this",
)


def _normalize_spec_candidate(text: str) -> str:
    candidate = text.strip()
    if not candidate:
        return ""
    lowered = candidate.lower()
    if any(phrase in lowered for phrase in _DISALLOWED_FALLBACK_PHRASES):
        return ""

    lines = candidate.splitlines()
    start_index = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if start_index is None:
        return ""
    candidate = "\n".join(lines[start_index:]).strip()
    if not all(header in candidate for header in _REQUIRED_SPEC_HEADERS):
        return ""
    return candidate


def _require_dict(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Planner wrote invalid {name}: expected a JSON object.")
    return value


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Planner wrote invalid {name}: expected a non-empty string.")
    return value.strip()


def _require_non_empty_list(name: str, value: Any) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"Planner wrote invalid {name}: expected a non-empty array.")
    return value


def _validate_design_tokens(tokens: dict[str, Any]) -> None:
    required_keys = (
        "theme_name",
        "color",
        "typography",
        "spacing",
        "radius",
        "motion",
        "style_rules",
        "anti_patterns",
    )
    for key in required_keys:
        if key not in tokens:
            raise RuntimeError(f"Planner wrote invalid design_tokens.json: missing '{key}'.")

    _require_non_empty_string("design_tokens.json.theme_name", tokens["theme_name"])
    _require_dict("design_tokens.json.color", tokens["color"])
    _require_dict("design_tokens.json.typography", tokens["typography"])
    _require_dict("design_tokens.json.spacing", tokens["spacing"])
    _require_dict("design_tokens.json.radius", tokens["radius"])
    _require_dict("design_tokens.json.motion", tokens["motion"])
    _require_non_empty_list("design_tokens.json.style_rules", tokens["style_rules"])
    if not isinstance(tokens["anti_patterns"], list):
        raise RuntimeError("Planner wrote invalid design_tokens.json: anti_patterns must be an array.")


def _validate_feature_list(feature_list: dict[str, Any]) -> None:
    features = _require_non_empty_list("feature_list.json.features", feature_list.get("features"))
    required_keys = (
        "id",
        "name",
        "priority",
        "depends_on",
        "description",
        "acceptance_criteria",
        "status",
        "sprint",
    )

    for index, feature in enumerate(features, start=1):
        feature_dict = _require_dict(f"feature_list.json.features[{index}]", feature)
        for key in required_keys:
            if key not in feature_dict:
                raise RuntimeError(
                    f"Planner wrote invalid feature_list.json: feature {index} is missing '{key}'."
                )
        _require_non_empty_string(f"feature_list.json.features[{index}].id", feature_dict["id"])
        _require_non_empty_string(f"feature_list.json.features[{index}].name", feature_dict["name"])
        _require_non_empty_string(
            f"feature_list.json.features[{index}].description", feature_dict["description"]
        )
        _require_non_empty_string(
            f"feature_list.json.features[{index}].priority", feature_dict["priority"]
        )
        if not isinstance(feature_dict["depends_on"], list):
            raise RuntimeError(
                f"Planner wrote invalid feature_list.json: feature {index} depends_on must be an array."
            )
        _require_non_empty_list(
            f"feature_list.json.features[{index}].acceptance_criteria",
            feature_dict["acceptance_criteria"],
        )
        if feature_dict["status"] != "planned":
            raise RuntimeError(
                f"Planner wrote invalid feature_list.json: feature {index} status must be 'planned'."
            )
        if not isinstance(feature_dict["sprint"], int) or feature_dict["sprint"] < 1:
            raise RuntimeError(
                f"Planner wrote invalid feature_list.json: feature {index} sprint must be a positive integer."
            )


def _validate_sprint_plan(sprint_plan: dict[str, Any]) -> None:
    total_sprints = sprint_plan.get("total_sprints")
    if not isinstance(total_sprints, int) or total_sprints < 1:
        raise RuntimeError(
            "Planner wrote invalid sprint_plan.json: total_sprints must be a positive integer."
        )

    sprints = _require_non_empty_list("sprint_plan.json.sprints", sprint_plan.get("sprints"))
    required_keys = ("number", "title", "goal", "feature_ids", "deliverables", "exit_criteria")

    for index, sprint in enumerate(sprints, start=1):
        sprint_dict = _require_dict(f"sprint_plan.json.sprints[{index}]", sprint)
        for key in required_keys:
            if key not in sprint_dict:
                raise RuntimeError(
                    f"Planner wrote invalid sprint_plan.json: sprint {index} is missing '{key}'."
                )
        if not isinstance(sprint_dict["number"], int) or sprint_dict["number"] < 1:
            raise RuntimeError(
                f"Planner wrote invalid sprint_plan.json: sprint {index} number must be a positive integer."
            )
        _require_non_empty_string(f"sprint_plan.json.sprints[{index}].title", sprint_dict["title"])
        _require_non_empty_string(f"sprint_plan.json.sprints[{index}].goal", sprint_dict["goal"])
        _require_non_empty_list(
            f"sprint_plan.json.sprints[{index}].feature_ids", sprint_dict["feature_ids"]
        )
        _require_non_empty_list(
            f"sprint_plan.json.sprints[{index}].deliverables", sprint_dict["deliverables"]
        )
        _require_non_empty_list(
            f"sprint_plan.json.sprints[{index}].exit_criteria", sprint_dict["exit_criteria"]
        )


def _validate_ui_verification_plan(verification_plan: dict[str, Any]) -> None:
    sprints = _require_non_empty_list(
        "ui_verification_plan.json.sprints", verification_plan.get("sprints")
    )
    required_keys = ("id", "feature_id", "task", "expected_result", "critical", "category")

    for sprint_index, sprint in enumerate(sprints, start=1):
        sprint_dict = _require_dict(f"ui_verification_plan.json.sprints[{sprint_index}]", sprint)
        if not isinstance(sprint_dict.get("sprint"), int) or sprint_dict["sprint"] < 1:
            raise RuntimeError(
                f"Planner wrote invalid ui_verification_plan.json: sprint {sprint_index} number must be a positive integer."
            )
        checks = _require_non_empty_list(
            f"ui_verification_plan.json.sprints[{sprint_index}].checks",
            sprint_dict.get("checks"),
        )
        for check_index, check in enumerate(checks, start=1):
            check_dict = _require_dict(
                f"ui_verification_plan.json.sprints[{sprint_index}].checks[{check_index}]",
                check,
            )
            for key in required_keys:
                if key not in check_dict:
                    raise RuntimeError(
                        "Planner wrote invalid ui_verification_plan.json: "
                        f"check {check_index} in sprint {sprint_index} is missing '{key}'."
                    )
            _require_non_empty_string(
                f"ui_verification_plan.json check {check_index} id", check_dict["id"]
            )
            _require_non_empty_string(
                f"ui_verification_plan.json check {check_index} feature_id", check_dict["feature_id"]
            )
            _require_non_empty_string(
                f"ui_verification_plan.json check {check_index} task", check_dict["task"]
            )
            _require_non_empty_string(
                f"ui_verification_plan.json check {check_index} expected_result",
                check_dict["expected_result"],
            )
            if not isinstance(check_dict["critical"], bool):
                raise RuntimeError(
                    "Planner wrote invalid ui_verification_plan.json: "
                    f"check {check_index} in sprint {sprint_index} critical must be a boolean."
                )
            _require_non_empty_string(
                f"ui_verification_plan.json check {check_index} category", check_dict["category"]
            )


def _validate_planning_bundle(file_comm: FileComm) -> dict[str, Any]:
    spec = file_comm.read_spec()
    if not spec:
        raise RuntimeError("Planner completed without writing .harness/spec.md.")
    if not all(header in spec for header in _REQUIRED_SPEC_HEADERS):
        raise RuntimeError("Planner wrote invalid spec.md: required sections are missing.")

    design_tokens = file_comm.read_design_tokens()
    if design_tokens is None:
        raise RuntimeError("Planner completed without writing .harness/design_tokens.json.")
    _validate_design_tokens(_require_dict("design_tokens.json", design_tokens))

    feature_list = file_comm.read_feature_list()
    if feature_list is None:
        raise RuntimeError("Planner completed without writing .harness/feature_list.json.")
    _validate_feature_list(_require_dict("feature_list.json", feature_list))

    sprint_plan = file_comm.read_sprint_plan()
    if sprint_plan is None:
        raise RuntimeError("Planner completed without writing .harness/sprint_plan.json.")
    _validate_sprint_plan(_require_dict("sprint_plan.json", sprint_plan))

    verification_plan = file_comm.read_ui_verification_plan()
    if verification_plan is None:
        raise RuntimeError("Planner completed without writing .harness/ui_verification_plan.json.")
    _validate_ui_verification_plan(_require_dict("ui_verification_plan.json", verification_plan))

    progress = file_comm.read_progress()
    if not progress.strip():
        raise RuntimeError("Planner completed without writing .harness/progress.md.")

    _validate_planning_cross_references(
        feature_list=feature_list,
        sprint_plan=sprint_plan,
        verification_plan=verification_plan,
    )

    return sprint_plan


def _validate_planning_cross_references(
    *,
    feature_list: dict[str, Any],
    sprint_plan: dict[str, Any],
    verification_plan: dict[str, Any],
) -> None:
    """Catch dangling references across the three plan files.

    A planner that wrote individually-valid files but referenced a
    feature_id that does not exist in feature_list, or assigned a
    feature to a sprint number outside ``total_sprints``, would have
    passed the per-file validators above. The evaluator and generator
    later degrade silently in that situation (``feature_id="unknown"``,
    repair with no direction). Catch it at planner boundary instead.
    """
    feature_ids: set[str] = {
        str(feature["id"])
        for feature in feature_list["features"]
        if isinstance(feature, dict) and feature.get("id")
    }
    total_sprints = int(sprint_plan["total_sprints"])
    valid_sprint_numbers = set(range(1, total_sprints + 1))

    # 1. feature.sprint must point inside the sprint range.
    for feature in feature_list["features"]:
        if not isinstance(feature, dict):
            continue
        sprint_num = feature.get("sprint")
        if not isinstance(sprint_num, int) or sprint_num not in valid_sprint_numbers:
            raise RuntimeError(
                f"Planner cross-ref failed: feature {feature.get('id')!r} is "
                f"assigned to sprint {sprint_num!r} which is outside "
                f"1..{total_sprints}."
            )

    # 2. sprint_plan must declare every sprint number 1..total_sprints exactly once.
    declared_sprint_numbers = [
        sprint.get("number")
        for sprint in sprint_plan["sprints"]
        if isinstance(sprint, dict)
    ]
    if sorted(n for n in declared_sprint_numbers if isinstance(n, int)) != sorted(valid_sprint_numbers):
        raise RuntimeError(
            "Planner cross-ref failed: sprint_plan.sprints must declare each "
            f"sprint number 1..{total_sprints} exactly once; got "
            f"{declared_sprint_numbers}."
        )

    # 3. sprint.feature_ids ⊆ feature_list.
    for sprint in sprint_plan["sprints"]:
        if not isinstance(sprint, dict):
            continue
        for feature_id in sprint.get("feature_ids", []) or []:
            if str(feature_id) not in feature_ids:
                raise RuntimeError(
                    f"Planner cross-ref failed: sprint {sprint.get('number')} "
                    f"references unknown feature_id {feature_id!r}."
                )

    # 4. ui_verification_plan checks must reference real features.
    for sprint in verification_plan["sprints"]:
        if not isinstance(sprint, dict):
            continue
        for check in sprint.get("checks", []) or []:
            if not isinstance(check, dict):
                continue
            ref_id = check.get("feature_id")
            if str(ref_id) not in feature_ids:
                raise RuntimeError(
                    f"Planner cross-ref failed: ui_verification_plan check "
                    f"{check.get('id')!r} references unknown feature_id "
                    f"{ref_id!r}."
                )


def _initialize_accepted_sprints(file_comm: FileComm, sprint_plan: dict[str, Any]) -> None:
    if file_comm.read_accepted_sprints() is not None:
        return
    total_sprints = sprint_plan["total_sprints"]
    current_target = 1 if total_sprints > 0 else 0
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": current_target,
            "last_evaluated_round": 0,
        }
    )


async def run_planner(
    config: HarnessConfig,
    user_prompt: str,
    file_comm: FileComm,
    workdir: Path,
) -> AgentRunStats:
    """Run the planner agent. Returns execution stats."""
    logger.info(f"[bold blue]Planner[/] starting for prompt: {user_prompt[:80]}...")
    workdir.mkdir(parents=True, exist_ok=True)
    file_comm.dir.mkdir(parents=True, exist_ok=True)

    prompt = (
        f"Create a complete planning bundle for this product idea:\n\n"
        f"{user_prompt}\n\n"
        f"Write all required planning artifacts into .harness using only file editing tools such as "
        f"Write, Edit, and MultiEdit. Bash is unavailable for this task. "
        f"The Harness has already prepared the workdir and .harness directory for this task, "
        f"so begin by writing the files themselves instead of creating directories. "
        f"Use paths relative to the workdir only; do not use absolute paths. "
        f"The workdir is: {workdir}"
    )

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt=prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        max_turns=30,
        allow_bash=False,
        trace_path=file_comm.dir / "traces" / "planner.jsonl",
    )

    spec = file_comm.read_spec()
    if not spec:
        fallback_spec = _normalize_spec_candidate(assistant_text)
        if not fallback_spec:
            fallback_spec = _normalize_spec_candidate(result.result or "")
        if fallback_spec:
            file_comm.write_spec(fallback_spec)
            logger.warning(
                "[bold blue]Planner[/] spec.md was not written by agent; "
                "used final result text as fallback."
            )

    sprint_plan = _validate_planning_bundle(file_comm)
    _initialize_accepted_sprints(file_comm, sprint_plan)

    if permission_denials:
        logger.warning(
            f"[bold blue]Planner[/] completed with permission denials: {permission_denials}"
        )

    logger.info(f"[bold blue]Planner[/] done. Cost: ${cost:.4f}")
    return build_agent_run_stats(result, model=config.planner_model)
