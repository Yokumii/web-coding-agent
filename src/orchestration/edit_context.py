"""Harness-selected source windows for low-token Edit and Repair calls."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.orchestration.minimal_path_guidance import CODE_EXTENSIONS


EDIT_CONTEXT_VERSION = "edit-context-v1"
_OUTLINE_LINE = re.compile(
    r"<(?:main|section|article|header|footer|nav|form|h[1-6])\b|"
    r"\b(?:id|data-testid|data-page)=|"
    r"^\s*(?:function|class)\s+[A-Za-z_$]|"
    r"\baddEventListener\s*\(",
    re.IGNORECASE,
)


def edit_context_name(round_num: int) -> str:
    return f"edit_context_round_{round_num}.json"


def _source_files(workdir: Path) -> list[Path]:
    frontend = workdir / "frontend"
    return sorted(
        path
        for path in frontend.rglob("*")
        if path.is_file()
        and path.suffix.lower() in CODE_EXTENSIONS
        and ".git" not in path.parts
        and "node_modules" not in path.parts
    )


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _bounded_window(
    lines: list[str], *, start: int, end: int, focus: int, max_chars: int
) -> tuple[int, int, str]:
    selected = {min(max(focus, start), end)}
    left = min(selected) - 1
    right = max(selected) + 1
    while True:
        candidates = [item for item in (left, right) if start <= item <= end]
        if not candidates:
            break
        added = False
        for line_number in candidates:
            projected = sum(len(lines[item - 1]) for item in selected | {line_number})
            if projected <= max_chars:
                selected.add(line_number)
                added = True
            if line_number == left:
                left -= 1
            else:
                right += 1
        if not added:
            break
    bounded_start, bounded_end = min(selected), max(selected)
    return bounded_start, bounded_end, "".join(lines[bounded_start - 1 : bounded_end])


def ensure_edit_context(
    *,
    workdir: Path,
    harness_dir: Path,
    plan: dict[str, Any],
    round_num: int,
    max_total_chars: int = 12_000,
    max_file_chars: int = 6_000,
    context_lines: int = 18,
    source_anchors: list[str] | None = None,
) -> dict[str, Any]:
    """Select deterministic code windows; never ask an LLM to discover source scope."""
    path = harness_dir / edit_context_name(round_num)
    cone = plan.get("source_change_cone") or {}
    hotspot_by_path = {
        str(item.get("path")): item
        for item in cone.get("hotspots") or []
        if isinstance(item, dict) and item.get("path")
    }
    initial_paths = [str(item) for item in cone.get("initial_paths") or []]
    source_anchors = [str(item) for item in source_anchors or [] if str(item).strip()]
    all_files = _source_files(workdir)
    full_source_chars = sum(
        len(item.read_text(encoding="utf-8", errors="replace")) for item in all_files
    )
    windows: list[dict[str, Any]] = []
    outlines: list[dict[str, Any]] = []
    anchored_paths: set[str] = set()
    remaining = max(1, max_total_chars)
    outline_remaining = max(256, min(4_000, max_total_chars // 3))
    for relative in initial_paths:
        source = workdir / relative
        if not source.is_file() or remaining <= 0:
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        candidate_outline_entries = [
            {"line": index, "content": line.rstrip("\r\n")}
            for index, line in enumerate(lines, start=1)
            if _OUTLINE_LINE.search(line)
        ][:160]
        outline_entries = []
        for entry in candidate_outline_entries:
            size = len(entry["content"])
            if size > outline_remaining:
                continue
            outline_entries.append(entry)
            outline_remaining -= size
        if outline_entries:
            outlines.append({"path": relative, "entries": outline_entries})
        anchor_lines = [
            index
            for index, line in enumerate(lines, start=1)
            if any(anchor in line for anchor in source_anchors)
        ]
        if anchor_lines:
            anchored_paths.add(relative)
        if len(content) <= min(max_file_chars, remaining):
            ranges = [(1, max(1, len(lines)))]
        else:
            line_numbers = [
                int(match.get("line"))
                for match in (hotspot_by_path.get(relative, {}).get("matches") or [])
                if isinstance(match, dict) and str(match.get("line", "")).isdigit()
            ]
            line_numbers.extend(anchor_lines)
            if not line_numbers:
                line_numbers = [1]
            ranges = _merge_ranges(
                [
                    (max(1, line - context_lines), min(len(lines), line + context_lines))
                    for line in line_numbers[:4]
                ]
            )
        used_for_file = 0
        for start, end in ranges:
            snippet = "".join(lines[start - 1 : end])
            allowed = min(remaining, max_file_chars - used_for_file)
            if allowed <= 0:
                break
            if len(snippet) > allowed:
                focus = next(
                    (line for line in line_numbers if start <= line <= end),
                    start,
                )
                start, end, snippet = _bounded_window(
                    lines,
                    start=start,
                    end=end,
                    focus=focus,
                    max_chars=allowed,
                )
            if not snippet:
                continue
            windows.append(
                {
                    "path": relative,
                    "start_line": start,
                    "end_line": end,
                    "sha256": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                    "content": snippet,
                }
            )
            remaining -= len(snippet)
            used_for_file += len(snippet)
    window_chars = sum(len(item["content"]) for item in windows)
    rendered_outline_paths = sorted(
        str(item["path"]) for item in outlines if str(item["path"]) not in anchored_paths
    )
    outline_chars = sum(
        len(entry["content"])
        for outline in outlines
        if str(outline["path"]) in rendered_outline_paths
        for entry in outline["entries"]
    )
    exposed_source_chars = window_chars + outline_chars
    payload = {
        "schema_version": EDIT_CONTEXT_VERSION,
        "owner": "harness",
        "round": round_num,
        "mode": plan.get("mode"),
        "source_windows": windows,
        "source_outlines": outlines,
        "rendered_outline_paths": rendered_outline_paths,
        "exposure": {
            "full_source_chars": full_source_chars,
            "exposed_source_chars": exposed_source_chars,
            "ratio": (
                round(exposed_source_chars / full_source_chars, 6)
                if full_source_chars
                else 0.0
            ),
            "selected_paths": sorted({item["path"] for item in windows}),
            "full_source_file_count": len(all_files),
            "window_chars": window_chars,
            "outline_chars": outline_chars,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def read_edit_context(harness_dir: Path, round_num: int) -> dict[str, Any] | None:
    path = harness_dir / edit_context_name(round_num)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != EDIT_CONTEXT_VERSION:
        return None
    return payload


def render_edit_context(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    blocks = []
    for item in payload.get("source_windows") or []:
        blocks.append(
            f"### {item['path']} lines {item['start_line']}-{item['end_line']}\n"
            f"```\n{item['content']}\n```"
        )
    rendered_outline_paths = set(payload.get("rendered_outline_paths") or [])
    for outline in payload.get("source_outlines") or []:
        if rendered_outline_paths and outline.get("path") not in rendered_outline_paths:
            continue
        if "rendered_outline_paths" in payload and not rendered_outline_paths:
            continue
        entries = "\n".join(
            f"{item['line']}: {item['content']}" for item in outline.get("entries") or []
        )
        blocks.append(f"### {outline['path']} structural outline\n```\n{entries}\n```")
    exposure = payload.get("exposure") or {}
    return (
        "## Harness-selected source context\n\n"
        + "\n\n".join(blocks)
        + "\n\nSource exposure: "
        + f"{exposure.get('exposed_source_chars', 0)}/{exposure.get('full_source_chars', 0)} chars "
        + f"({float(exposure.get('ratio', 0.0)) * 100:.1f}%)."
    )


__all__ = [
    "EDIT_CONTEXT_VERSION",
    "edit_context_name",
    "ensure_edit_context",
    "read_edit_context",
    "render_edit_context",
]
