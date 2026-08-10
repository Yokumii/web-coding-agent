#!/usr/bin/env python3
"""Audit whether forward v2 edit/repair records satisfy reverse-data gates."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


# Measured from the authoritative 20260806 WebCompass v2 release, not a
# hand-written eligibility range.  Keep this compact reference in the audit so
# calibration batches can be evaluated without copying the 300MB source JSONL.
REVERSE_REFERENCE = {
    "text-editing": {
        "count": 3000,
        "task_count_distribution": {1: 429, 2: 429, 3: 428, 4: 428, 5: 428, 6: 429, 7: 429},
        "patch_count_distribution": {1: 2, 2: 16, 3: 234, 4: 94, 5: 74, 6: 206, 7: 138, 8: 96, 9: 191, 10: 128, 11: 121, 12: 160, 13: 123, 14: 132, 15: 175, 16: 143, 17: 140, 18: 197, 19: 132, 20: 138, 21: 131, 22: 92, 23: 56, 24: 35, 25: 19, 26: 8, 27: 9, 28: 4, 29: 2, 30: 2, 31: 2},
        "changed_file_distribution": {1: 6, 2: 87, 3: 2900, 4: 6, 5: 1},
    },
    "text-repair": {
        "count": 3332,
        "task_count_distribution": {1: 476, 2: 476, 3: 476, 4: 476, 5: 476, 6: 476, 7: 476},
        "patch_count_distribution": {1: 426, 2: 422, 3: 469, 4: 453, 5: 456, 6: 479, 7: 468, 8: 117, 9: 28, 10: 12, 11: 2},
        "changed_file_distribution": {1: 1357, 2: 1889, 3: 86},
    },
}


def _tv_distance(observed: dict[int, int], reference: dict[int, int]) -> float:
    observed_total = sum(observed.values())
    reference_total = sum(reference.values())
    if not observed_total or not reference_total:
        return 1.0
    keys = set(observed) | set(reference)
    return round(0.5 * sum(abs(observed.get(k, 0) / observed_total - reference.get(k, 0) / reference_total) for k in keys), 4)


def _integer_targets(reference: dict[int, int], sample_size: int) -> dict[int, int]:
    """Allocate a finite calibration batch by largest remainder."""
    total = sum(reference.values())
    exact = {key: sample_size * count / total for key, count in reference.items()}
    result = {key: int(value) for key, value in exact.items()}
    for key, _ in sorted(exact.items(), key=lambda item: (item[1] - int(item[1]), -item[0]), reverse=True)[:sample_size - sum(result.values())]:
        result[key] += 1
    return result


def _deficits(observed: dict[int, int], reference: dict[int, int], sample_size: int) -> dict[int, int]:
    targets = _integer_targets(reference, sample_size)
    return {key: max(0, targets[key] - observed.get(key, 0)) for key in sorted(targets)}


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
        "reverse_shape_eligible": sum(1 for tc in task_counts if 1 <= tc <= 7),
        "contract_errors": contract_errors,
    }


def audit(forward_dir: Path, reference_edit: Path | None = None,
          reference_repair: Path | None = None, target_sample_size: int = 100) -> dict[str, Any]:
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
    else:
        result["reverse_reference"] = REVERSE_REFERENCE
    alignment: dict[str, Any] = {}
    for task in ("text-editing", "text-repair"):
        observed = result["forward"][task]
        reference = result["reverse_reference"][task]
        alignment[task] = {
            "sample_size_sufficient_for_distribution_claim": observed["count"] >= 100,
            "task_count_tv_distance": _tv_distance(observed["task_count_distribution"], reference["task_count_distribution"]),
            "patch_count_tv_distance": _tv_distance(observed["patch_count_distribution"], reference["patch_count_distribution"]),
            "changed_file_tv_distance": _tv_distance(observed["changed_file_distribution"], reference["changed_file_distribution"]),
            "calibration_deficits_to_target_size": {
                "target_sample_size": target_sample_size,
                "task_count": _deficits(observed["task_count_distribution"], reference["task_count_distribution"], target_sample_size),
                "patch_count": _deficits(observed["patch_count_distribution"], reference["patch_count_distribution"], target_sample_size),
                "changed_files": _deficits(observed["changed_file_distribution"], reference["changed_file_distribution"], target_sample_size),
            },
        }
    result["alignment"] = alignment
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-v2-dir", type=Path, required=True)
    parser.add_argument("--reference-edit", type=Path)
    parser.add_argument("--reference-repair", type=Path)
    parser.add_argument("--target-sample-size", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.forward_v2_dir.is_dir():
        parser.error(f"forward v2 directory does not exist: {args.forward_v2_dir}")
    if args.target_sample_size < 1:
        parser.error("target sample size must be positive")
    payload = audit(args.forward_v2_dir, args.reference_edit, args.reference_repair, args.target_sample_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "forward": payload["forward"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
