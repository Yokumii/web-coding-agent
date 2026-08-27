from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.export_run_folders import export_records_to_folders


def test_folder_export_uses_verified_record_labels_without_reclassification(tmp_path: Path):
    screenshot = tmp_path / "source.png"
    screenshot.write_bytes(b"png")
    record = {
        "instance_id": "case__edit_s01",
        "task": "text-editing",
        "status": "ok",
        "task_type": ["Search"],
        "description": "Add search.",
        "instruction": {"src_code": [{"path": "src/App.jsx", "code": "before\n"}]},
        "reference": {"dst_code": [{"path": "src/App.jsx", "code": "after\n"}]},
        "label_modified_files": [{
            "path": "src/App.jsx", "search": "before", "replace": "after",
            "task_type": "Search",
        }],
        "images": {"src_screenshot": [{"path": str(screenshot)}], "dst_screenshot": []},
        "quality": {"tier": "benchmark_aligned"},
        "trajectory": {"source_commit": "a", "destination_commit": "b"},
    }

    created = export_records_to_folders([record], tmp_path / "run", tmp_path / "view")

    folder = created[0]
    assert folder.name.endswith("text-editing")
    assert (folder / "input_code" / "src" / "App.jsx").read_text() == "before\n"
    assert (folder / "reference_code" / "src" / "App.jsx").read_text() == "after\n"
    assert json.loads((folder / "record.json").read_text())["task"] == "text-editing"
    assert (folder / "images" / "source" / "source.png").is_file()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        export_records_to_folders([record], tmp_path / "run", tmp_path / "view")
