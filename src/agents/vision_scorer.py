from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib import error, request

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.pricing import estimate_cost_usd
from src.prompts.evaluator_vision import EVALUATOR_VISION_SYSTEM_PROMPT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"

# Upstream / proxy errors that we treat as transient and retry. Everything
# else (4xx, JSON parse failure, etc.) raises immediately so we don't mask
# real bugs behind silent retries.
_RETRYABLE_HTTP_STATUS = frozenset({500, 502, 503, 504, 520, 521, 522, 524, 529})

# Patterns that look like API credentials in upstream error bodies. We
# do NOT want these in trace files or harness logs.
_SECRET_REGEX = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]+|x-api-key:\s*\S+)",
    re.IGNORECASE,
)


def _scrub_secrets(text: str, *, limit: int = 512) -> str:
    """Truncate and redact a string before logging or raising it.

    Used on upstream HTTP error bodies (which some proxies echo request
    headers back into) and any other detail that may contain bearer
    tokens or x-api-key values.
    """
    truncated = text[:limit]
    if len(text) > limit:
        truncated = truncated + "...[truncated]"
    return _SECRET_REGEX.sub("***", truncated)


def _validate_screenshot_path(relative_path: str, workdir: Path) -> Path:
    """Reject screenshot paths that escape workdir / aren't .png /
    aren't under .harness/.

    Without this, a manifest written by a compromised evaluator could
    point at arbitrary files (``.aws/credentials`` etc.) which the
    vision scorer would then base64-encode and POST to the external
    vision endpoint.
    """
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


def _validate_design_reference_path(relative_path: str, workdir: Path) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(
            f"design reference path must stay under workdir: {relative_path!r}"
        )

    workdir_resolved = workdir.resolve()
    resolved = (workdir_resolved / candidate).resolve()
    try:
        resolved.relative_to(workdir_resolved / ".harness" / "design")
    except ValueError as exc:
        raise ValueError(
            f"design reference image must live under .harness/design/: {relative_path!r}"
        ) from exc

    if resolved.suffix.lower() != ".png":
        raise ValueError(
            f"design reference image must have .png extension: {relative_path!r}"
        )
    return resolved


def _collect_existing_design_reference_paths(
    file_comm: FileComm,
    workdir: Path,
) -> list[str]:
    candidates: list[str] = []
    design_brief = file_comm.read_design_brief() or {}
    reference_files = design_brief.get("reference_files")
    if isinstance(reference_files, dict):
        candidates.extend(str(path) for path in reference_files.values() if str(path).strip())

    asset_manifest = file_comm.read_asset_manifest() or {}
    assets = asset_manifest.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            usage = str(asset.get("usage", "")).strip()
            if usage not in {"visual_reference", "full_bleed_background"}:
                continue
            path = str(asset.get("path", "")).strip()
            if path:
                candidates.append(path)

    references: list[str] = []
    seen: set[str] = set()
    for relative_path in candidates:
        if relative_path in seen:
            continue
        try:
            absolute_path = _validate_design_reference_path(relative_path, workdir)
        except ValueError:
            continue
        if absolute_path.exists():
            references.append(relative_path)
            seen.add(relative_path)
    return references


def _normalize_endpoint_type(endpoint_type: str) -> str:
    normalized = endpoint_type.strip().lower()
    if normalized in {"anthropic", "openai"}:
        return normalized
    raise ValueError(f"unsupported evaluator vision endpoint type: {endpoint_type}")


def _build_messages_url(base_url: str) -> str:
    normalized = (base_url or _DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def _build_chat_completions_url(base_url: str) -> str:
    normalized = (base_url or _DEFAULT_OPENAI_BASE_URL).rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _read_image_as_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


_FENCED_JSON_RE = re.compile(
    r"```(?:json|JSON)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_json_relaxations(blob: str) -> str:
    """Strip common LLM-introduced violations of strict JSON.

    Handles:
      - ``// line comments`` and ``/* block comments */``
      - trailing commas before ``}`` or ``]``
    """
    cleaned = _BLOCK_COMMENT_RE.sub("", blob)
    cleaned = _LINE_COMMENT_RE.sub("", cleaned)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    return cleaned


def _extract_balanced_json_blobs(text: str) -> list[str]:
    """Yield every top-level balanced ``{...}`` block, ignoring braces in strings.

    The previous implementation used ``find('{')`` + ``rfind('}')`` which
    over-grabs whenever the model emits multiple JSON-shaped blocks (e.g.
    an example schema followed by the actual answer) — the slice ends up
    spanning both, with text in between, and json.loads chokes.
    """
    blobs: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                blobs.append(text[start : i + 1])
                start = -1
    return blobs


def _extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from a model response.

    Tries, in order:
      1. ```json fenced``` blocks (strict, then relaxed)
      2. Top-level balanced ``{...}`` blocks, longest first (strict, then relaxed)
      3. The full ``find('{')..rfind('}')`` slice (strict, then relaxed)

    On total failure, raises with a redacted snippet of the raw text so
    the caller's log line shows what the model actually returned.
    """
    candidates: list[str] = []
    for match in _FENCED_JSON_RE.finditer(text):
        candidates.append(match.group(1))

    # Schema-example-then-answer is common: prefer the longer block, which is
    # almost always the real answer. Ties broken by original order (stable sort).
    balanced = _extract_balanced_json_blobs(text)
    balanced.sort(key=len, reverse=True)
    candidates.extend(balanced)

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    last_error: Exception | None = None
    for blob in candidates:
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            last_error = exc
        try:
            return json.loads(_strip_json_relaxations(blob))
        except json.JSONDecodeError as exc:
            last_error = exc

    snippet = _scrub_secrets(text, limit=400)
    if last_error is not None:
        raise ValueError(
            f"vision response was not valid JSON ({last_error}); raw text: {snippet}"
        )
    raise ValueError(
        f"vision response did not contain a JSON object; raw text: {snippet}"
    )


def _build_review_context(
    *,
    file_comm: FileComm,
    sprint_num: int,
    sprint_context: dict[str, Any],
    screenshot_names: list[str],
    design_reference_names: list[str] | None = None,
) -> str:
    spec_text = file_comm.read_spec().strip()
    design_tokens = file_comm.read_design_tokens() or {}
    design_brief = file_comm.read_design_brief()
    layout_contract = file_comm.read_layout_contract()
    asset_manifest = file_comm.read_asset_manifest()
    has_design_contract = design_brief is not None

    payload = {
        "task": "Evaluate the visual appearance of the current sprint screenshots.",
        "sprint": sprint_num,
        "sprint_title": sprint_context.get("title", "Unknown Sprint"),
        "sprint_goal": sprint_context.get("goal", ""),
        "deliverables": sprint_context.get("deliverables", []),
        "exit_criteria": sprint_context.get("exit_criteria", []),
        "screenshots": screenshot_names,
        "design_reference_images": design_reference_names or [],
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
            "Use the full 1-5 scale for appearance_review fields.",
            "Use the full 0-10 scale for criteria_scores.",
            "Return only valid JSON.",
        ],
    }
    if has_design_contract:
        payload["design_contract"] = {
            "design_brief": design_brief,
            "layout_contract": layout_contract or {},
            "asset_manifest": asset_manifest or {"assets": []},
        }
        payload["instructions"].insert(
            1,
            "When a design contract is present, judge whether the screenshots preserve its declared visual strategy, hierarchy, and overlay intent.",
        )
        if design_reference_names:
            payload["instructions"].insert(
                2,
                "The request includes design reference images after the screenshots; compare the implemented screenshots against those references without requiring pixel-perfect duplication.",
            )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _coerce_rating(value: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    if isinstance(value, (int, float)):
        integer = int(round(float(value)))
        return max(minimum, min(maximum, integer))
    return fallback


def _coerce_score(value: Any, fallback: float) -> float:
    """Clamp a numeric score into [0, 10] with one decimal of precision.

    Non-numeric / NaN / inf values fall back to ``fallback``. Callers in the
    vision pipeline pass ``fallback=0.0`` so that malformed responses fail
    closed.
    """
    # bool is a subclass of int in Python; treat it as non-numeric here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    score = float(value)
    if score != score or score in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return max(0.0, min(10.0, round(score, 1)))


def _build_stats_from_http_response(
    *,
    duration_ms: int,
    usage: dict[str, Any],
    model: str,
) -> AgentRunStats:
    token_usage = {
        key: value
        for key, value in usage.items()
        if isinstance(value, int) and "token" in key
    }
    return AgentRunStats(
        cost_usd=estimate_cost_usd(model, token_usage),
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        token_usage=token_usage,
        usage=usage.copy(),
        model_usage={"model": model},
    )


def _build_anthropic_content_blocks(
    *,
    workdir: Path,
    screenshot_paths: list[str],
    reference_image_paths: list[str] | None = None,
    review_context: str,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": review_context}]
    for relative_path in screenshot_paths:
        absolute_path = _validate_screenshot_path(relative_path, workdir)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _read_image_as_base64(absolute_path),
                },
            }
        )
    for relative_path in reference_image_paths or []:
        absolute_path = _validate_design_reference_path(relative_path, workdir)
        content.append({"type": "text", "text": f"Design reference image: {relative_path}"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _read_image_as_base64(absolute_path),
                },
            }
        )
    return content


def _build_openai_content_blocks(
    *,
    workdir: Path,
    screenshot_paths: list[str],
    reference_image_paths: list[str] | None = None,
    review_context: str,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": review_context}]
    for relative_path in screenshot_paths:
        absolute_path = _validate_screenshot_path(relative_path, workdir)
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{_read_image_as_base64(absolute_path)}"
                },
            }
        )
    for relative_path in reference_image_paths or []:
        absolute_path = _validate_design_reference_path(relative_path, workdir)
        content.append({"type": "text", "text": f"Design reference image: {relative_path}"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{_read_image_as_base64(absolute_path)}"
                },
            }
        )
    return content


def _build_anthropic_request(
    *,
    config: HarnessConfig,
    workdir: Path,
    screenshot_paths: list[str],
    reference_image_paths: list[str] | None = None,
    review_context: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    return (
        _build_messages_url(config.evaluator_vision_base_url),
        {
            "content-type": "application/json",
            "x-api-key": config.evaluator_vision_api_key,
            "anthropic-version": "2023-06-01",
        },
        {
            "model": config.evaluator_vision_model,
            "max_tokens": config.evaluator_vision_max_tokens,
            "system": EVALUATOR_VISION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": _build_anthropic_content_blocks(
                        workdir=workdir,
                        screenshot_paths=screenshot_paths,
                        reference_image_paths=reference_image_paths,
                        review_context=review_context,
                    ),
                }
            ],
        },
    )


def _build_openai_request(
    *,
    config: HarnessConfig,
    workdir: Path,
    screenshot_paths: list[str],
    reference_image_paths: list[str] | None = None,
    review_context: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    return (
        _build_chat_completions_url(config.evaluator_vision_base_url),
        {
            "content-type": "application/json",
            "authorization": f"Bearer {config.evaluator_vision_api_key}",
        },
        {
            "model": config.evaluator_vision_model,
            "max_tokens": config.evaluator_vision_max_tokens,
            "messages": [
                {"role": "system", "content": EVALUATOR_VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_openai_content_blocks(
                        workdir=workdir,
                        screenshot_paths=screenshot_paths,
                        reference_image_paths=reference_image_paths,
                        review_context=review_context,
                    ),
                },
            ],
        },
    )


def _extract_openai_message_text(parsed: dict[str, Any]) -> str:
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("vision response did not contain choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("vision response choice did not contain a message")
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in text_parts if part)
    raise ValueError("vision response message content had an unsupported shape")


def _extract_anthropic_message_text(parsed: dict[str, Any]) -> str:
    text_parts = [
        block.get("text", "")
        for block in parsed.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in text_parts if part)


def _urlopen_with_retries(
    *,
    endpoint: str,
    body: bytes,
    headers: dict[str, str],
    max_retries: int,
    base_delay: float,
) -> str:
    """POST ``body`` to ``endpoint`` and retry on transient upstream failures.

    Retries on HTTP 5xx / proxy 5xx codes in ``_RETRYABLE_HTTP_STATUS`` and on
    socket-level ``URLError`` (connection reset, DNS, timeout). 4xx and other
    errors raise immediately. ``max_retries`` is the number of additional
    attempts after the first try, so ``max_retries=3`` means up to 4 calls.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            http_request = request.Request(endpoint, data=body, headers=headers, method="POST")
            with request.urlopen(http_request, timeout=90) as response:
                return response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = _scrub_secrets(exc.read().decode("utf-8", errors="replace"))
            wrapped = RuntimeError(f"vision scorer HTTP {exc.code}: {detail}")
            wrapped.__cause__ = exc
            if exc.code not in _RETRYABLE_HTTP_STATUS or attempt >= max_retries:
                raise wrapped from exc
            last_exc = wrapped
        except error.URLError as exc:
            wrapped = RuntimeError(
                f"vision scorer connection failed: {_scrub_secrets(str(exc.reason))}"
            )
            wrapped.__cause__ = exc
            if attempt >= max_retries:
                raise wrapped from exc
            last_exc = wrapped

        delay = base_delay * (2 ** attempt)
        if delay > 0:
            delay *= 1.0 + random.uniform(-0.25, 0.25)
        logger.warning(
            f"vision scorer attempt {attempt + 1}/{max_retries + 1} failed: {last_exc}; "
            f"retrying in {delay:.1f}s"
        )
        time.sleep(max(0.0, delay))

    # Defensive: the loop should always exit via return-on-success or raise.
    raise last_exc if last_exc else RuntimeError("vision scorer retry loop exited unexpectedly")


def _perform_visual_review_request(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    sprint_num: int,
    sprint_context: dict[str, Any],
    screenshot_paths: list[str],
) -> tuple[dict[str, Any], AgentRunStats]:
    if not config.evaluator_vision_model:
        raise ValueError("missing evaluator vision model")
    if not config.evaluator_vision_api_key:
        raise ValueError("missing evaluator vision API key")

    reference_image_paths = _collect_existing_design_reference_paths(file_comm, workdir)
    review_context = _build_review_context(
        file_comm=file_comm,
        sprint_num=sprint_num,
        sprint_context=sprint_context,
        screenshot_names=screenshot_paths,
        design_reference_names=reference_image_paths,
    )
    endpoint_type = _normalize_endpoint_type(config.evaluator_vision_endpoint_type)
    if endpoint_type == "anthropic":
        endpoint, headers, payload = _build_anthropic_request(
            config=config,
            workdir=workdir,
            screenshot_paths=screenshot_paths,
            reference_image_paths=reference_image_paths,
            review_context=review_context,
        )
    else:
        endpoint, headers, payload = _build_openai_request(
            config=config,
            workdir=workdir,
            screenshot_paths=screenshot_paths,
            reference_image_paths=reference_image_paths,
            review_context=review_context,
        )

    body = json.dumps(payload).encode("utf-8")
    started = time.perf_counter()

    response_body = _urlopen_with_retries(
        endpoint=endpoint,
        body=body,
        headers=headers,
        max_retries=max(0, int(config.evaluator_vision_max_retries)),
        base_delay=max(0.0, float(config.evaluator_vision_retry_base_delay_seconds)),
    )

    duration_ms = int((time.perf_counter() - started) * 1000)
    parsed = json.loads(response_body)
    if endpoint_type == "anthropic":
        response_text = _extract_anthropic_message_text(parsed)
    else:
        response_text = _extract_openai_message_text(parsed)
    review = _extract_json_object(response_text)
    stats = _build_stats_from_http_response(
        duration_ms=duration_ms,
        usage=parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {},
        model=config.evaluator_vision_model,
    )
    # Preserve the endpoint hint for trace consumers; the bare model
    # name is used for pricing lookup above.
    stats = replace(
        stats,
        model_usage={
            **stats.model_usage,
            "endpoint_type": endpoint_type,
            "model": f"{config.evaluator_vision_model}:{endpoint_type}",
        },
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
    appearance = review.get("appearance_review")
    if not isinstance(appearance, dict):
        appearance = {}

    criteria_scores = review.get("criteria_scores")
    if not isinstance(criteria_scores, dict):
        criteria_scores = {}

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
            # Fallbacks are 0.0 (not the per-criterion threshold) so that
            # missing or malformed responses fail closed in check_grades.
            "design_quality": {
                "score": _coerce_score(
                    (criteria_scores.get("design_quality") or {}).get("score"),
                    fallback=0.0,
                ),
                "notes": str((criteria_scores.get("design_quality") or {}).get("notes", "")).strip(),
            },
            "originality": {
                "score": _coerce_score(
                    (criteria_scores.get("originality") or {}).get("score"),
                    fallback=0.0,
                ),
                "notes": str((criteria_scores.get("originality") or {}).get("notes", "")).strip(),
            },
            "craft": {
                "score": _coerce_score(
                    (criteria_scores.get("craft") or {}).get("score"),
                    fallback=0.0,
                ),
                "notes": str((criteria_scores.get("craft") or {}).get("notes", "")).strip(),
            },
        },
    }
    if normalized["phase_result"] not in {"pass", "fail"}:
        normalized["phase_result"] = "pass"
    return normalized
