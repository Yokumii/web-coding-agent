#!/usr/bin/env python3
"""Render strictly exported trajectory records into human-browsable folders."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.export_trajectory_dataset import export_run


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:100] or "record"


def _safe_destination(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe record path: {relative}")
    destination = (root / path).resolve()
    destination.relative_to(root.resolve())
    return destination


def _write_code(root: Path, items: list[dict[str, str]]) -> None:
    for item in items:
        destination = _safe_destination(root, str(item["path"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(item.get("code", "")), encoding="utf-8")


def _copy_images(folder: Path, images: dict[str, Any]) -> None:
    for key, label in (
        ("input_images", "input"),
        ("src_screenshot", "source"),
        ("dst_screenshot", "destination"),
    ):
        for index, item in enumerate(images.get(key) or []):
            source = Path(str(item.get("path", "")))
            if not source.is_file():
                continue
            target_dir = folder / "images" / label
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if target.exists():
                target = target_dir / f"{index:02d}_{source.name}"
            shutil.copy2(source, target)


def _copy_evidence(run_dir: Path, folder: Path) -> None:
    harness = run_dir / ".harness"
    if not harness.is_dir():
        return
    patterns = (
        "grade_round_*.json", "browser_evidence_round_*.json",
        "edit_dom_guard_round_*.json", "minimality_round_*.json",
        "minimal_path_plan_round_*.json", "edit_scope_round_*.json",
    )
    selected = sorted({path for pattern in patterns for path in harness.glob(pattern)})
    if not selected:
        return
    evidence = folder / "evidence"
    evidence.mkdir()
    for source in selected:
        shutil.copy2(source, evidence / source.name)


def export_records_to_folders(
    records: list[dict[str, Any]], run_dir: Path, output_dir: Path
) -> list[Path]:
    """Materialize exporter-approved records without inferring new task labels."""
    accepted = [record for record in records if record.get("status") == "ok"]
    names = [
        f"{index:03d}_{_slug(str(record['instance_id']))}_{_slug(str(record['task']))}"
        for index, record in enumerate(accepted)
    ]
    output_dir = output_dir.resolve()
    existing = [output_dir / name for name in names if (output_dir / name).exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite existing folder: {existing[0]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, record in zip(names, accepted, strict=True):
        folder = output_dir / name
        folder.mkdir()
        (folder / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        task_types = ", ".join(str(item) for item in record.get("task_type") or [])
        (folder / "task.md").write_text(
            f"# {record['instance_id']}\n\n"
            f"- Exported task: `{record['task']}`\n"
            f"- Task types: {task_types or '(none)'}\n\n"
            f"## Description\n\n{record.get('description', '')}\n",
            encoding="utf-8",
        )
        _write_code(folder / "input_code", (record.get("instruction") or {}).get("src_code") or [])
        _write_code(folder / "reference_code", (record.get("reference") or {}).get("dst_code") or [])
        (folder / "patches.json").write_text(
            json.dumps(record.get("label_modified_files") or [], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _copy_images(folder, record.get("images") or {})
        _copy_evidence(run_dir, folder)
        created.append(folder)
    return created


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a human-readable view from the strict trajectory exporter"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    records = export_run(args.run_dir.resolve())
    folders = export_records_to_folders(records, args.run_dir.resolve(), args.output_dir)
    print(json.dumps({
        "records": len(records), "folders": len(folders),
        "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
