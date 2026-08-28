#!/usr/bin/env python3
"""Curate strict trajectory records without treating every checkpoint as Generate.

The strict exporter remains the source of truth for trajectory/evidence validity. This
post-export layer only removes redundant Generate views and admits an intermediate or
second Generate when a semantic reviewer (human or model) explicitly records that it is
materially different from both the initial and previously selected Generate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any


SELECTION_SCHEMA = "generate-materiality-selection-v1"
GENERATION_ROLES = {"checkpoint_generate", "complete_generate"}
FAMILY_FILES = {
    "text-generation": "generate.jsonl",
    "text-editing": "edit.jsonl",
    "text-repair": "repair.jsonl",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"records JSONL does not exist: {path}")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line {line_number}: {exc}") from exc
        if not isinstance(item, dict) or not isinstance(item.get("instance_id"), str):
            raise ValueError(f"record line {line_number} has no string instance_id")
        instance_id = str(item["instance_id"])
        if instance_id in seen:
            raise ValueError(f"duplicate record instance_id: {instance_id}")
        seen.add(instance_id)
        records.append(item)
    return records


def _validated_decisions(
    selection: dict[str, Any], record_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if selection.get("schema_version") != SELECTION_SCHEMA:
        raise ValueError(f"selection schema must be {SELECTION_SCHEMA}")
    raw_decisions = selection.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ValueError("selection decisions must be a list")
    decisions: dict[str, dict[str, Any]] = {}
    for raw in raw_decisions:
        if not isinstance(raw, dict) or not isinstance(raw.get("instance_id"), str):
            raise ValueError("every materiality decision needs a string instance_id")
        instance_id = str(raw["instance_id"])
        if instance_id not in record_ids:
            raise ValueError(f"materiality decision targets unknown record: {instance_id}")
        if instance_id in decisions:
            raise ValueError(f"duplicate materiality decision: {instance_id}")
        status = raw.get("status")
        if status not in {"pass", "fail"}:
            raise ValueError(f"materiality decision status must be pass/fail: {instance_id}")
        reviewer = raw.get("reviewer")
        if not isinstance(reviewer, dict) or reviewer.get("kind") not in {
            "semantic_review",
            "human",
        }:
            raise ValueError(
                f"materiality reviewer must be semantic_review or human: {instance_id}"
            )
        evidence = raw.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(item, str) and item.strip() for item in evidence
        ):
            raise ValueError(f"materiality evidence must be non-empty strings: {instance_id}")
        if status == "pass" and (
            raw.get("significant_vs_initial") is not True
            or raw.get("significant_vs_previous_selected") is not True
            or not isinstance(raw.get("reference_instance_id"), str)
        ):
            raise ValueError(
                "passing materiality decisions require both significance flags and "
                f"a reference_instance_id: {instance_id}"
            )
        decisions[instance_id] = copy.deepcopy(raw)
    return decisions


def _checkpoint_index(record: dict[str, Any]) -> int:
    try:
        return int((record.get("quality") or {}).get("checkpoint_index"))
    except (TypeError, ValueError):
        raise ValueError(f"Generate record has no checkpoint_index: {record.get('instance_id')}")


def _destination_commit(record: dict[str, Any]) -> str:
    commit = (record.get("trajectory") or {}).get("destination_commit")
    if not isinstance(commit, str) or not commit:
        raise ValueError(
            f"Generate record has no destination commit: {record.get('instance_id')}"
        )
    return commit


def _with_selection(
    record: dict[str, Any], decision: dict[str, Any] | None, reason: str
) -> dict[str, Any]:
    output = copy.deepcopy(record)
    quality = output.setdefault("quality", {})
    quality["generate_materiality_selection"] = {
        "schema_version": SELECTION_SCHEMA,
        "status": "pass" if decision else "structural_anchor",
        "reason": reason,
        **({"decision": decision} if decision else {}),
    }
    return output


def _select_trajectory_generates(
    records: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> tuple[set[str], dict[str, dict[str, Any] | None], list[dict[str, str]]]:
    checkpoints = sorted(
        (
            record
            for record in records
            if (record.get("quality") or {}).get("trajectory_role")
            == "checkpoint_generate"
        ),
        key=_checkpoint_index,
    )
    completes = [
        record
        for record in records
        if (record.get("quality") or {}).get("trajectory_role") == "complete_generate"
    ]
    if len(completes) > 1:
        raise ValueError("one trajectory cannot contain multiple complete_generate records")
    complete = completes[0] if completes else None
    keep: set[str] = set()
    selected_decisions: dict[str, dict[str, Any] | None] = {}
    excluded: list[dict[str, str]] = []

    terminal_commit = _destination_commit(complete) if complete else None
    duplicate_terminal_ids = {
        str(record["instance_id"])
        for record in checkpoints
        if terminal_commit and _destination_commit(record) == terminal_commit
    }
    selectable = [
        record
        for record in checkpoints
        if str(record["instance_id"]) not in duplicate_terminal_ids
    ]

    if not complete:
        if selectable:
            initial = selectable[0]
            initial_id = str(initial["instance_id"])
            keep.add(initial_id)
            selected_decisions[initial_id] = None
            previous_id = initial_id
            for candidate in selectable[1:]:
                candidate_id = str(candidate["instance_id"])
                decision = decisions.get(candidate_id)
                if (
                    decision
                    and decision.get("status") == "pass"
                    and decision.get("reference_instance_id") == previous_id
                ):
                    keep.add(candidate_id)
                    selected_decisions[candidate_id] = decision
                    previous_id = candidate_id
                else:
                    excluded.append(
                        {
                            "instance_id": candidate_id,
                            "reason": "no_passing_materiality_decision",
                        }
                    )
        return keep, selected_decisions, excluded

    complete_id = str(complete["instance_id"])
    complete_decision = decisions.get(complete_id)
    initial = selectable[0] if selectable else None
    if initial and complete_decision and complete_decision.get("status") == "pass":
        initial_id = str(initial["instance_id"])
        keep.add(initial_id)
        selected_decisions[initial_id] = None
        previous_id = initial_id
        for candidate in selectable[1:]:
            candidate_id = str(candidate["instance_id"])
            decision = decisions.get(candidate_id)
            if (
                decision
                and decision.get("status") == "pass"
                and decision.get("reference_instance_id") == previous_id
            ):
                keep.add(candidate_id)
                selected_decisions[candidate_id] = decision
                previous_id = candidate_id
            else:
                excluded.append(
                    {
                        "instance_id": candidate_id,
                        "reason": "no_passing_materiality_decision",
                    }
                )
        if complete_decision.get("reference_instance_id") != previous_id:
            raise ValueError(
                f"complete Generate decision must reference the previous selected Generate: "
                f"{complete_id} -> {previous_id}"
            )
    elif initial:
        excluded.extend(
            {
                "instance_id": str(candidate["instance_id"]),
                "reason": "complete_not_proven_significant_vs_initial",
            }
            for candidate in selectable
        )

    for candidate_id in duplicate_terminal_ids:
        excluded.append(
            {
                "instance_id": candidate_id,
                "reason": "duplicate_destination_with_complete_generate",
            }
        )
    keep.add(complete_id)
    selected_decisions[complete_id] = (
        complete_decision if complete_decision and complete_decision.get("status") == "pass" else None
    )
    return keep, selected_decisions, excluded


def curate_records(
    records: list[dict[str, Any]], selection: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    record_ids = {
        str(record.get("instance_id"))
        for record in records
        if isinstance(record.get("instance_id"), str)
    }
    if len(record_ids) != len(records):
        raise ValueError("records must have unique string instance_id values")
    decisions = _validated_decisions(selection, record_ids)
    generation_by_trajectory: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("status") != "ok":
            raise ValueError(f"curation accepts only status=ok: {record.get('instance_id')}")
        if record.get("task") != "text-generation":
            continue
        quality = record.get("quality") or {}
        role = quality.get("trajectory_role")
        if role not in GENERATION_ROLES:
            raise ValueError(f"unsupported Generate trajectory role: {role}")
        trajectory_id = quality.get("parent_trajectory_id")
        if not isinstance(trajectory_id, str) or not trajectory_id:
            raise ValueError(f"Generate record has no parent trajectory: {record['instance_id']}")
        generation_by_trajectory.setdefault(trajectory_id, []).append(record)
    generation_ids = {
        str(record["instance_id"])
        for trajectory_records in generation_by_trajectory.values()
        for record in trajectory_records
    }
    non_generation_decisions = sorted(set(decisions) - generation_ids)
    if non_generation_decisions:
        raise ValueError(
            "materiality decisions cannot target non-Generate records: "
            + ", ".join(non_generation_decisions)
        )

    keep: set[str] = {
        str(record["instance_id"])
        for record in records
        if record.get("task") != "text-generation"
    }
    selected_decisions: dict[str, dict[str, Any] | None] = {}
    excluded: list[dict[str, str]] = []
    for trajectory_records in generation_by_trajectory.values():
        selected, selected_for_records, removed = _select_trajectory_generates(
            trajectory_records, decisions
        )
        keep.update(selected)
        selected_decisions.update(selected_for_records)
        excluded.extend(removed)

    curated: list[dict[str, Any]] = []
    for record in records:
        instance_id = str(record["instance_id"])
        if instance_id not in keep:
            continue
        if record.get("task") != "text-generation":
            curated.append(copy.deepcopy(record))
            continue
        role = (record.get("quality") or {}).get("trajectory_role")
        decision = selected_decisions.get(instance_id)
        reason = (
            "initial_generate"
            if role == "checkpoint_generate" and decision is None
            else "terminal_complete_generate"
            if role == "complete_generate"
            else "semantically_material_checkpoint"
        )
        curated.append(_with_selection(record, decision, reason))

    excluded.sort(key=lambda item: next(
        index
        for index, record in enumerate(records)
        if record.get("instance_id") == item["instance_id"]
    ))
    counts = {
        task: sum(record.get("task") == task for record in curated)
        for task in ("text-generation", "text-editing", "text-repair")
    }
    return curated, {
        "schema_version": SELECTION_SCHEMA,
        "counts": counts,
        "selected_instance_ids": [record["instance_id"] for record in curated],
        "excluded": excluded,
    }


def write_curated_release(
    records_path: Path,
    output_dir: Path,
    selection: dict[str, Any],
) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError(f"refusing to overwrite existing release: {output_dir}")
    records = _read_jsonl(records_path)
    curated, report = curate_records(records, selection)
    output_dir.mkdir(parents=True)

    def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    write_rows(output_dir / "records.jsonl", curated)
    for task, filename in FAMILY_FILES.items():
        family = [record for record in curated if record.get("task") == task]
        if family:
            write_rows(output_dir / filename, family)
    manifest = {
        **report,
        "source_records": str(records_path.resolve()),
        "source_records_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "selection": copy.deepcopy(selection),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    try:
        manifest = write_curated_release(args.records, args.output_dir, selection)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(manifest["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
