#!/usr/bin/env python3
"""Audit whether forward v2 edit/repair records satisfy reverse-data gates."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _code(record: dict[str, Any]) -> list[dict[str, str]]:
    if record["task"] == "text-editing":
        return record["instruction"]["src_code"]
    return record["instruction"]


def _summary(rows: list[dict[str, Any]], task: str) -> dict[str, Any]:
    records = [row for row in rows if row.get("task") == task]
    patches = [row.get("response", []) for row in records]
    task_counts = [len(row.get("task_type", [])) for row in records]
    file_counts = [len({patch.get("path") for patch in group}) for group in patches]
    patch_counts = [len(group) for group in patches]
    code_sizes = [sum(len(item.get("code", "")) for item in _code(row)) for row in records]
    contract_errors = []
    for row in records:
        if task == "text-repair" and not isinstance(row.get("instruction"), list):
            contract_errors.append(f"{row.get('instance_id')}: repair instruction must be code list")
        if task == "text-editing":
            instruction = row.get("instruction") or {}
            if not str(instruction.get("description", "")).strip():
                contract_errors.append(f"{row.get('instance_id')}: edit description missing")
        for patch in row.get("response", []):
            if not patch.get("search") or patch.get("search") == patch.get("replace"):
                contract_errors.append(f"{row.get('instance_id')}: invalid exact patch")
    return {
        "count": len(records),
        "task_count_distribution": dict(Counter(task_counts)),
        "patch_count_distribution": dict(Counter(patch_counts)),
        "changed_file_distribution": dict(Counter(file_counts)),
        "code_chars": {"min": min(code_sizes, default=0), "max": max(code_sizes, default=0)},
        "reverse_shape_eligible": sum(
            1 for tc, pc in zip(task_counts, patch_counts)
            if 1 <= tc <= 7 and 1 <= pc <= 10
        ),
        "contract_errors": contract_errors,
    }


def audit(forward_dir: Path, reference_edit: Path | None = None,
          reference_repair: Path | None = None) -> dict[str, Any]:
    forward = []
    for filename in ("text-edit.v2.jsonl", "text-repair.v2.jsonl"):
        forward.extend(_rows(forward_dir / filename))
    result = {
        "status": "ok",
        "forward": {
            "text-editing": _summary(forward, "text-editing"),
            "text-repair": _summary(forward, "text-repair"),
            "image_edit_count": len(_rows(forward_dir / "image-edit.v2.jsonl")),
            "image_repair_count": len(_rows(forward_dir / "image-repair.v2.jsonl")),
        },
    }
    if reference_edit or reference_repair:
        reference = []
        if reference_edit:
            reference.extend(_rows(reference_edit))
        if reference_repair:
            reference.extend(_rows(reference_repair))
        result["reverse_reference"] = {
            "text-editing": _summary(reference, "text-editing"),
            "text-repair": _summary(reference, "text-repair"),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-v2-dir", type=Path, required=True)
    parser.add_argument("--reference-edit", type=Path)
    parser.add_argument("--reference-repair", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = audit(args.forward_v2_dir, args.reference_edit, args.reference_repair)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "forward": payload["forward"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
