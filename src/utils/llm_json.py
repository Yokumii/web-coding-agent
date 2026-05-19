"""从 LLM 自由文本响应中提取 JSON 对象的统一实现。"""
from __future__ import annotations

import json
import re
from typing import Any


class LLMJSONError(ValueError):
    """LLM 响应无法解析为 JSON 对象时抛出。"""


_FENCED_JSON_RE = re.compile(
    r"```(?:json|JSON)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_json_relaxations(blob: str) -> str:
    """清理模型常见的 JSON 轻微格式问题。"""
    cleaned = _BLOCK_COMMENT_RE.sub("", blob)
    cleaned = _LINE_COMMENT_RE.sub("", cleaned)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    return cleaned


def _extract_balanced_json_blobs(text: str) -> list[str]:
    """提取文本中所有顶层平衡的 `{...}` 片段，并忽略字符串内部的大括号。"""
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


def _truncate_snippet(text: str, limit: int = 400) -> str:
    """将错误片段裁剪到固定长度，避免日志过长。"""
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _load_json_candidates(blob: str, *, allow_relaxed: bool) -> tuple[list[dict[str, Any]], Exception | None]:
    """按严格模式与宽松模式依次尝试解析单个候选片段。"""
    candidates = [blob]
    if allow_relaxed:
        relaxed = _strip_json_relaxations(blob)
        if relaxed != blob:
            candidates.append(relaxed)

    parsed_objects: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            parsed_objects.append(parsed)
    return parsed_objects, last_error


def extract_json_object(
    text: str,
    *,
    allow_relaxed: bool = True,
) -> dict[str, Any]:
    """按多种候选策略，从模型响应中尽力提取首个 JSON 对象。"""
    candidates: list[str] = []
    for match in _FENCED_JSON_RE.finditer(text):
        candidates.append(match.group(1))

    # “先举例 schema，再给最终答案”较常见，这里优先尝试更长的对象块。
    balanced = _extract_balanced_json_blobs(text)
    balanced.sort(key=len, reverse=True)
    candidates.extend(balanced)

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    last_error: Exception | None = None
    for blob in candidates:
        parsed_objects, parse_error = _load_json_candidates(
            blob,
            allow_relaxed=allow_relaxed,
        )
        if parsed_objects:
            return parsed_objects[0]
        if parse_error is not None:
            last_error = parse_error

    snippet = _truncate_snippet(text, limit=400)
    if last_error is not None:
        raise LLMJSONError(
            f"LLM response was not valid JSON ({last_error}); raw text: {snippet}"
        )
    raise LLMJSONError(
        f"LLM response did not contain a JSON object; raw text: {snippet}"
    )
