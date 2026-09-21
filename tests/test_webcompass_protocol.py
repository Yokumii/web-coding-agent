from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from scripts.export_trajectory_dataset import apply_patches
from src.orchestration.webcompass_protocol import (
    EDIT_INSTRUCTION_PROMPT,
    IMAGE_GENERATION_PROMPT,
    REPAIR_INSTRUCTION_PROMPT,
    TEXT_GENERATION_PROMPT,
    markdown_repository,
    model_io_view,
    official_patches,
    parse_markdown_repository,
    search_replace_xml,
    validate_edit_repair_record,
    validate_generation_record,
)


def _edit_record(task: str = "edit") -> dict:
    types = (
        ["Data Table", "Page Transitions", "Drag & Drop Interface", "Real-time Dashboard"]
        if task == "edit"
        else ["Overflow", "Color Contrast", "Loss of Interactivity", "Missing Attributes"]
    )
    return {
        "instance_id": "case-1",
        "task": task,
        "task_type": types,
        "difficulty": "medium",
        "description": [
            {"task_type": task_type, "description": f"private description {index}"}
            for index, task_type in enumerate(types)
        ],
        "src_code": [{"path": "index.html", "code": "<main>before</main>"}],
        "dst_code": [{"path": "index.html", "code": "<main>after</main>"}],
        "src_screenshot": ["before.jpg"],
        "dst_screenshot": ["after.jpg"] if task == "repair" else [],
        "label_modified_files": [
            {"path": "index.html", "search": "before", "replace": "after"}
        ],
        "resources": [],
    }


def test_generation_records_keep_text_and_image_inputs_distinct():
    common = {
        "repo": "harness/webcoding",
        "instance_id": "g1",
        "base_commit": "abc",
        "problem_statement": [],
        "meta": {},
        "working_dir": "/testbed",
    }
    validate_generation_record({**common, "instruction": "design"}, modality="text")
    validate_generation_record(common, modality="image")

    with pytest.raises(ValueError, match="do not carry an instruction"):
        validate_generation_record({**common, "instruction": "design"}, modality="image")


def test_edit_exposes_descriptions_but_never_target_code():
    record = _edit_record("edit")
    view = model_io_view(record, task_name="image-editing", image_paths=["before.jpg"])

    assert "private description 0" in view["model_visible_text"]
    assert "<main>before</main>" in view["model_visible_text"]
    assert "<main>after</main>" not in view["model_visible_text"]
    assert view["model_visible_images"] == ["before.jpg"]


def test_repair_hides_instance_descriptions_and_adds_target_image_only_in_visual_mode():
    record = _edit_record("repair")
    text_view = model_io_view(record, task_name="diagnostic-repair")
    visual_view = model_io_view(
        record,
        task_name="visual-diagnostic-repair",
        image_paths=["before.jpg"],
        target_image_paths=["after.jpg"],
    )

    assert "private description" not in text_view["model_visible_text"]
    assert "You have only 4 issues" in text_view["model_visible_text"]
    assert text_view["model_visible_images"] == []
    assert visual_view["model_visible_images"] == ["before.jpg", "after.jpg"]
    assert "<main>after</main>" not in visual_view["model_visible_text"]
    content = visual_view["model_visible_content"]
    assert [item["type"] for item in content] == [
        "text", "text", "text", "image_url", "text", "text", "image_url"
    ]
    assert "Current State Screenshots" in content[1]["text"]
    assert "Target State Screenshots" in content[4]["text"]


def test_official_new_file_encoding_replays_without_weakening_native_patch():
    source = [{"path": "index.html", "code": "<main></main>"}]
    native = [{
        "path": "main.js",
        "operation": "create_file",
        "content": "console.log('ok')",
        "task_type": "Page Transitions",
    }]

    exported = official_patches(native)

    assert exported == [{
        "path": "main.js", "search": "", "replace": "console.log('ok')"
    }]
    assert apply_patches(source, exported) == [
        {"path": "index.html", "code": "<main></main>"},
        {"path": "main.js", "code": "console.log('ok')"},
    ]
    assert "<search>\n\n</search>" in search_replace_xml(native)


def test_markdown_generation_output_round_trips_complete_repository():
    code = [
        {"path": "index.html", "code": "<main>ok</main>\n"},
        {"path": "main.js", "code": "console.log('ok')\n"},
    ]
    assert parse_markdown_repository(markdown_repository(code)) == code


def test_compound_records_require_four_to_twelve_official_tasks():
    record = _edit_record("edit")
    record["task_type"] = ["Unknown"]
    record["description"] = [{"task_type": "Unknown", "description": "x"}]
    with pytest.raises(ValueError, match="between 4 and 12"):
        validate_edit_repair_record(record)


def test_prompt_copies_match_local_official_checkout_when_available():
    repo_root = Path(__file__).resolve().parents[1]
    official = repo_root.parent / "evaluate" / "WebCompass"
    edit_path = official / "editing_repair" / "llm" / "mllm" / "prompt.py"
    generation_path = official / "generation" / "prompts.py"
    if not edit_path.is_file() or not generation_path.is_file():
        pytest.skip("official sibling checkout is unavailable")

    edit_spec = importlib.util.spec_from_file_location("official_edit_prompt", edit_path)
    edit_module = importlib.util.module_from_spec(edit_spec)
    assert edit_spec.loader is not None
    edit_spec.loader.exec_module(edit_module)
    generation_spec = importlib.util.spec_from_file_location(
        "official_generation_prompt", generation_path
    )
    generation_module = importlib.util.module_from_spec(generation_spec)
    assert generation_spec.loader is not None
    generation_spec.loader.exec_module(generation_module)

    assert EDIT_INSTRUCTION_PROMPT == edit_module.Edit_Instruction_Prompt
    assert REPAIR_INSTRUCTION_PROMPT == edit_module.Repair_Instruction_Prompt
    assert TEXT_GENERATION_PROMPT == generation_module.TEXT_TO_WEB_PROMPT
    assert IMAGE_GENERATION_PROMPT == generation_module.IMAGE_TO_WEB_PROMPT
