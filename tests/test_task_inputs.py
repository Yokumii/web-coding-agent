from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from src.orchestration.task_inputs import (
    TaskInputError,
    anthropic_user_content,
    load_task_input_manifest,
    openai_user_content,
    stage_task_inputs,
    task_input_prompt_context,
)


def test_stage_task_inputs_materializes_manifest_and_preserves_types(tmp_path: Path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    notes = tmp_path / "requirements.md"
    notes.write_text("Keep the settings page unchanged.", encoding="utf-8")
    workdir = tmp_path / "run"

    manifest = stage_task_inputs(workdir, [image, notes])

    assert manifest["schema_version"] == "harness-task-inputs-v1"
    assert [item["kind"] for item in manifest["inputs"]] == ["image", "text"]
    assert all((workdir / item["staged_path"]).is_file() for item in manifest["inputs"])
    assert load_task_input_manifest(workdir) == manifest
    context = task_input_prompt_context(workdir)
    assert "Keep the settings page unchanged." in context
    assert ".harness/inputs/" in context


def test_stage_task_inputs_rejects_unsupported_binary(tmp_path: Path):
    binary = tmp_path / "payload.bin"
    binary.write_bytes(b"\x00\x01\x02")

    with pytest.raises(TaskInputError, match="unsupported input type"):
        stage_task_inputs(tmp_path / "run", [binary])


def test_task_input_context_rejects_manifest_path_escape(tmp_path: Path):
    notes = tmp_path / "requirements.md"
    notes.write_text("safe", encoding="utf-8")
    workdir = tmp_path / "run"
    manifest = stage_task_inputs(workdir, [notes])
    manifest["inputs"][0]["staged_path"] = "../requirements.md"
    (workdir / ".harness" / "task_inputs.json").write_text(json.dumps(manifest))

    with pytest.raises(TaskInputError, match="unsafe|escapes"):
        task_input_prompt_context(workdir)


def test_multimodal_message_builders_attach_real_image_bytes(tmp_path: Path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")

    openai = openai_user_content("Match this reference", [image])
    assert openai[0] == {"type": "text", "text": "Match this reference"}
    assert openai[1]["type"] == "image_url"
    assert openai[1]["image_url"]["url"].startswith("data:image/png;base64,")

    anthropic = anthropic_user_content("Match this reference", [image])
    assert anthropic[0] == {"type": "text", "text": "Match this reference"}
    assert anthropic[1]["type"] == "image"
    assert base64.b64decode(anthropic[1]["source"]["data"]) == image.read_bytes()
