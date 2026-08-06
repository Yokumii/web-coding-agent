from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.design_contract import DesignContractContext
from src.orchestration.file_comm import FileComm
from src.orchestration.pricing import estimate_cost_usd
from src.prompts.evaluator_vision import EVALUATOR_VISION_SYSTEM_PROMPT
from src.utils.llm_client import CompletionResult, completion
from src.utils.llm_json import extract_json_object
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _validate_screenshot_path(relative_path: str, workdir: Path) -> Path:
    """校验截图路径，限定在 `workdir/.harness` 下且后缀为 `.png`。"""
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(
            f"vision screenshot path must be relative to workdir: {relative_path!r}"
        )
    if ".." in candidate.parts:
        raise ValueError(
            f"vision screenshot path escapes workdir: {relative_path!r}"
        )

    workdir_resolved = workdir.resolve()
    resolved = (workdir_resolved / candidate).resolve()
    try:
        resolved.relative_to(workdir_resolved)
    except ValueError as exc:
        raise ValueError(
            f"vision screenshot path escapes workdir: {relative_path!r}"
        ) from exc

    if resolved.suffix.lower() != ".png":
        raise ValueError(
            f"vision screenshot must have .png extension: {relative_path!r}"
        )

    harness_dir = workdir_resolved / ".harness"
    try:
        resolved.relative_to(harness_dir)
    except ValueError as exc:
        raise ValueError(
            f"vision screenshot must live under .harness/: {relative_path!r}"
        ) from exc

    return resolved


def _read_image_as_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _build_review_context(
    *,
    file_comm: FileComm,
    sprint_num: int,
    sprint_context: dict[str, Any],
    screenshot_names: list[str],
) -> str:
    """将当前 sprint 的视觉审阅上下文整理为 JSON 文本。"""
    spec_text = file_comm.read_spec().strip()
    design_tokens = file_comm.read_design_tokens() or {}
    design_contract = DesignContractContext.load(file_comm)

    payload = {
        "task": "Evaluate the visual appearance of the current sprint screenshots.",
        "sprint": sprint_num,
        "sprint_title": sprint_context.get("title", "Unknown Sprint"),
        "sprint_goal": sprint_context.get("goal", ""),
        "deliverables": sprint_context.get("deliverables", []),
        "exit_criteria": sprint_context.get("exit_criteria", []),
        "screenshots": screenshot_names,
        "design_tokens": design_tokens,
        "spec_excerpt": spec_text[:8000],
        "response_schema": {
            "phase_result": "pass or fail",
            "appearance_review": {
                "render_stability": "integer 1-5",
                "content_relevance": "integer 1-5",
                "layout_harmony": "integer 1-5",
                "modernness_memorability": "integer 1-5",
                "token_adherence": "integer 1-5",
                "notes": "short paragraph",
            },
            "criteria_scores": {
                "design_quality": {"score": "number 0-10", "notes": "short paragraph"},
                "originality": {"score": "number 0-10", "notes": "short paragraph"},
                "craft": {"score": "number 0-10", "notes": "short paragraph"},
            },
        },
        "instructions": [
            "Judge the screenshots against the spec excerpt, design tokens, and sprint goal.",
            "If a design contract is present, also judge whether the implementation preserves its intended hierarchy and composition.",
            "Use the full 1-5 scale for appearance_review fields.",
            "Use the full 0-10 scale for criteria_scores.",
            "Return only valid JSON.",
        ],
    }
    design_contract_payload = design_contract.vision_payload()
    if design_contract_payload is not None:
        payload["design_contract"] = design_contract_payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _coerce_rating(value: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    """将评分值收敛到整数区间；异常值回退到默认值。"""
    if isinstance(value, (int, float)):
        integer = int(round(float(value)))
        return max(minimum, min(maximum, integer))
    return fallback


def _coerce_score(value: Any, fallback: float) -> float:
    """将分数收敛到 [0, 10]；异常值回退到默认值。"""
    # Python 中 bool 是 int 的子类，这里按非数值处理。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    score = float(value)
    if score != score or score in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return max(0.0, min(10.0, round(score, 1)))


def _provider_model_string(config: HarnessConfig) -> str:
    """按端点类型生成 LiteLLM 所需的 model 标识。"""
    endpoint_type = (config.evaluator_vision_endpoint_type or "anthropic").strip().lower()
    base = config.evaluator_vision_model
    if endpoint_type in ("", "anthropic"):
        return f"anthropic/{base}"
    if endpoint_type == "openai":
        return f"openai/{base}"
    raise ValueError(
        f"unsupported evaluator vision endpoint type: {config.evaluator_vision_endpoint_type!r}; "
        "expected 'anthropic' or 'openai'"
    )


def _build_vision_messages(
    *,
    workdir: Path,
    screenshot_paths: list[str],
    review_context: str,
) -> list[dict[str, Any]]:
    """构造 LiteLLM 共用的文本加图片消息体。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": review_context}]
    for relative_path in screenshot_paths:
        absolute_path = _validate_screenshot_path(relative_path, workdir)
        b64 = _read_image_as_base64(absolute_path)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
    return [
        {"role": "system", "content": EVALUATOR_VISION_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _build_stats_from_completion(
    *,
    duration_ms: int,
    usage: dict[str, int],
    model: str,
) -> AgentRunStats:
    """将 HTTP 视觉请求的 usage 信息整理为统一统计结构。"""
    token_usage = {key: value for key, value in usage.items() if "token" in key}
    return AgentRunStats(
        cost_usd=estimate_cost_usd(model, token_usage),
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        token_usage=token_usage,
        usage=dict(usage),
        model_usage={"model": model},
    )


def _perform_visual_review_request(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    sprint_num: int,
    sprint_context: dict[str, Any],
    screenshot_paths: list[str],
) -> tuple[dict[str, Any], AgentRunStats]:
    """执行一次视觉审阅请求，并解析返回的 JSON 结果。"""
    if not config.evaluator_vision_model:
        raise ValueError("missing evaluator vision model")
    if not config.evaluator_vision_api_key:
        raise ValueError("missing evaluator vision API key")

    review_context = _build_review_context(
        file_comm=file_comm,
        sprint_num=sprint_num,
        sprint_context=sprint_context,
        screenshot_names=[Path(p).name for p in screenshot_paths],
    )
    messages = _build_vision_messages(
        workdir=workdir,
        screenshot_paths=screenshot_paths,
        review_context=review_context,
    )

    started = time.perf_counter()
    result: CompletionResult = completion(
        messages=messages,
        model=_provider_model_string(config),
        api_key=config.evaluator_vision_api_key,
        api_base=config.evaluator_vision_base_url or None,
        max_tokens=config.evaluator_vision_max_tokens,
        num_retries=getattr(config, "evaluator_vision_max_retries", 3),
        timeout=getattr(config, "evaluator_vision_timeout_seconds", 300),
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    review = extract_json_object(result.text)
    stats = _build_stats_from_completion(
        duration_ms=duration_ms,
        usage=result.usage,
        model=config.evaluator_vision_model,
    )
    return review, stats


async def run_visual_appearance_review(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    sprint_num: int,
    sprint_context: dict[str, Any],
    screenshot_paths: list[str],
) -> tuple[dict[str, Any], AgentRunStats]:
    """在线程池中执行视觉审阅，避免阻塞事件循环。"""
    return await asyncio.to_thread(
        _perform_visual_review_request,
        config=config,
        file_comm=file_comm,
        workdir=workdir,
        sprint_num=sprint_num,
        sprint_context=sprint_context,
        screenshot_paths=screenshot_paths,
    )


def normalize_visual_review(
    review: dict[str, Any],
    screenshot_paths: list[str],
) -> dict[str, Any]:
    """将视觉模型的原始输出整理成评分系统可直接消费的结构。"""
    appearance = review.get("appearance_review")
    if not isinstance(appearance, dict):
        appearance = {}

    criteria_scores = review.get("criteria_scores")
    if not isinstance(criteria_scores, dict):
        criteria_scores = {}

    def criterion_value(name: str) -> tuple[Any, str]:
        raw = criteria_scores.get(name)
        if isinstance(raw, dict):
            return raw.get("score"), str(raw.get("notes", "")).strip()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return raw, ""
        return None, ""

    design_score, design_notes = criterion_value("design_quality")
    originality_score, originality_notes = criterion_value("originality")
    craft_score, craft_notes = criterion_value("craft")

    normalized = {
        "phase_result": str(review.get("phase_result", "pass")).strip().lower(),
        "appearance_review": {
            "screenshots": screenshot_paths,
            "render_stability": _coerce_rating(
                appearance.get("render_stability"),
                minimum=1,
                maximum=5,
                fallback=3,
            ),
            "content_relevance": _coerce_rating(
                appearance.get("content_relevance"),
                minimum=1,
                maximum=5,
                fallback=3,
            ),
            "layout_harmony": _coerce_rating(
                appearance.get("layout_harmony"),
                minimum=1,
                maximum=5,
                fallback=3,
            ),
            "modernness_memorability": _coerce_rating(
                appearance.get("modernness_memorability"),
                minimum=1,
                maximum=5,
                fallback=3,
            ),
            "token_adherence": _coerce_rating(
                appearance.get("token_adherence"),
                minimum=1,
                maximum=5,
                fallback=3,
            ),
            "notes": str(appearance.get("notes", "")).strip(),
        },
        "criteria_scores": {
            # 缺失或格式异常时一律按 0.0 处理，让评分逻辑向失败方向收敛。
            "design_quality": {
                "score": _coerce_score(design_score, fallback=0.0),
                "notes": design_notes,
            },
            "originality": {
                "score": _coerce_score(originality_score, fallback=0.0),
                "notes": originality_notes,
            },
            "craft": {
                "score": _coerce_score(craft_score, fallback=0.0),
                "notes": craft_notes,
            },
        },
    }
    if normalized["phase_result"] not in {"pass", "fail"}:
        normalized["phase_result"] = "pass"
    return normalized
