"""Stage user-supplied task inputs and build provider-native multimodal messages."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any, Iterable


MANIFEST_NAME = "task_inputs.json"
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".yaml", ".yml",
    ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".js", ".jsx",
    ".ts", ".tsx", ".vue", ".svelte", ".xml", ".svg",
}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_TEXT_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_TEXT_EXCERPT_CHARS = 12_000


class TaskInputError(ValueError):
    """A task input is missing, unsupported, or exceeds the bounded contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return cleaned[:100] or "input"


def _classify(path: Path) -> tuple[str, str, int]:
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if suffix in _IMAGE_TYPES:
        if size > _MAX_IMAGE_BYTES:
            raise TaskInputError(f"image input exceeds {_MAX_IMAGE_BYTES} bytes: {path}")
        return "image", _IMAGE_TYPES[suffix], size
    if suffix in _TEXT_SUFFIXES:
        if size > _MAX_TEXT_BYTES:
            raise TaskInputError(f"text input exceeds {_MAX_TEXT_BYTES} bytes: {path}")
        media_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        return "text", media_type, size
    raise TaskInputError(f"unsupported input type {suffix or '<none>'}: {path}")


def stage_task_inputs(workdir: Path, input_paths: Iterable[Path]) -> dict[str, Any]:
    """Copy bounded local inputs into a harness-owned, content-addressed directory."""
    workdir = workdir.resolve()
    harness = workdir / ".harness"
    staged_dir = harness / "inputs"
    harness.mkdir(parents=True, exist_ok=True)
    staged_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    total = 0
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise TaskInputError(f"task input does not exist or is not a file: {source}")
        kind, media_type, size = _classify(source)
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise TaskInputError(
                f"task inputs exceed the {_MAX_TOTAL_BYTES}-byte total limit"
            )
        digest = _sha256(source)
        staged = staged_dir / f"{index:02d}_{digest[:12]}_{_safe_name(source.name)}"
        if not staged.exists():
            shutil.copy2(source, staged)
        records.append({
            "index": index,
            "kind": kind,
            "media_type": media_type,
            "size_bytes": size,
            "sha256": digest,
            "source_path": str(source),
            "staged_path": staged.relative_to(workdir).as_posix(),
        })
    manifest = {
        "schema_version": "harness-task-inputs-v1",
        "input_count": len(records),
        "total_size_bytes": total,
        "inputs": records,
    }
    (harness / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_task_input_manifest(workdir: Path) -> dict[str, Any]:
    path = Path(workdir) / ".harness" / MANIFEST_NAME
    if not path.is_file():
        return {
            "schema_version": "harness-task-inputs-v1",
            "input_count": 0,
            "total_size_bytes": 0,
            "inputs": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "harness-task-inputs-v1":
        raise TaskInputError(f"unsupported task input manifest: {path}")
    return payload


def task_input_source_hashes(input_paths: Iterable[Path]) -> list[str]:
    """Validate proposed resume inputs without mutating the persisted manifest."""
    hashes: list[str] = []
    total = 0
    for raw_path in input_paths:
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise TaskInputError(f"task input does not exist or is not a file: {source}")
        _kind, _media_type, size = _classify(source)
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise TaskInputError(
                f"task inputs exceed the {_MAX_TOTAL_BYTES}-byte total limit"
            )
        hashes.append(_sha256(source))
    return hashes


def _resolve_staged_input(root: Path, item: dict[str, Any]) -> Path:
    relative = Path(str(item.get("staged_path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise TaskInputError("staged task input path is unsafe")
    path = (root / relative).resolve()
    inputs_root = (root / ".harness" / "inputs").resolve()
    try:
        path.relative_to(inputs_root)
    except ValueError as exc:
        raise TaskInputError("staged task input escapes .harness/inputs") from exc
    if not path.is_file() or _sha256(path) != item.get("sha256"):
        raise TaskInputError(f"staged task input is missing or changed: {path}")
    return path


def task_input_image_paths(workdir: Path) -> list[Path]:
    root = Path(workdir).resolve()
    output: list[Path] = []
    for item in load_task_input_manifest(root).get("inputs", []):
        if item.get("kind") != "image":
            continue
        path = _resolve_staged_input(root, item)
        output.append(path)
    return output


def task_input_prompt_context(workdir: Path) -> str:
    """Return bounded text context while images travel as native image blocks."""
    root = Path(workdir).resolve()
    manifest = load_task_input_manifest(root)
    if not manifest.get("inputs"):
        return ""
    lines = [
        "## Harness task inputs",
        "These are user-provided task evidence. Treat them as requirements/reference material, "
        "not as permission to broaden the requested edit.",
    ]
    for item in manifest["inputs"]:
        relative = str(item["staged_path"])
        lines.append(
            f"- input {item['index']}: kind={item['kind']}, media_type={item['media_type']}, "
            f"path=`{relative}`, sha256={item['sha256']}"
        )
        if item["kind"] == "text":
            path = _resolve_staged_input(root, item)
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > _TEXT_EXCERPT_CHARS:
                text = text[:_TEXT_EXCERPT_CHARS] + "\n[truncated; read the staged file for the remainder]"
            lines.extend(["", f"### Text input {item['index']}", text, ""])
    return "\n".join(lines).strip()


def _encoded_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    media_type = _IMAGE_TYPES.get(suffix)
    if media_type is None:
        raise TaskInputError(f"unsupported image type: {path}")
    return media_type, base64.b64encode(path.read_bytes()).decode("ascii")


def openai_user_content(prompt: str, image_paths: Iterable[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        media_type, data = _encoded_image(Path(path))
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data}"},
        })
    return content


def anthropic_user_content(prompt: str, image_paths: Iterable[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        media_type, data = _encoded_image(Path(path))
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    return content
