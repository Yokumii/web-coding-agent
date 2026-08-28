from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.curate_generate_materiality import curate_records, write_curated_release


def _record(
    instance_id: str,
    task: str,
    role: str,
    checkpoint: int,
    commit: str,
) -> dict:
    return {
        "instance_id": instance_id,
        "task": task,
        "status": "ok",
        "description": instance_id,
        "trajectory": {
            "source_commit": None,
            "destination_commit": commit,
        },
        "quality": {
            "trajectory_role": role,
            "parent_trajectory_id": "trajectory-1",
            "checkpoint_index": checkpoint,
        },
    }


def _decision(instance_id: str, reference: str) -> dict:
    return {
        "instance_id": instance_id,
        "status": "pass",
        "reference_instance_id": reference,
        "significant_vs_initial": True,
        "significant_vs_previous_selected": True,
        "reviewer": {"kind": "semantic_review", "name": "reviewer-1"},
        "evidence": ["Adds a new owned page and independently tested interaction."],
    }


def test_curator_removes_terminal_checkpoint_duplicate_and_keeps_three_families():
    rows = [
        _record("edit-s2", "text-editing", "canonical_edit", 2, "c2"),
        _record("repair-r5-r9", "text-repair", "natural_repair", 2, "c2"),
        _record("generate-s1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("generate-s2", "text-generation", "checkpoint_generate", 2, "c2"),
        _record("generate-complete", "text-generation", "complete_generate", 2, "c2"),
    ]
    selection = {
        "schema_version": "generate-materiality-selection-v1",
        "decisions": [_decision("generate-complete", "generate-s1")],
    }

    curated, report = curate_records(rows, selection)

    assert [row["instance_id"] for row in curated] == [
        "edit-s2",
        "repair-r5-r9",
        "generate-s1",
        "generate-complete",
    ]
    assert report["counts"] == {
        "text-generation": 2,
        "text-editing": 1,
        "text-repair": 1,
    }
    assert report["excluded"] == [
        {
            "instance_id": "generate-s2",
            "reason": "duplicate_destination_with_complete_generate",
        }
    ]
    complete = curated[-1]
    assert complete["quality"]["generate_materiality_selection"]["status"] == "pass"


def test_intermediate_generate_requires_explicit_semantic_materiality():
    rows = [
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("g2", "text-generation", "checkpoint_generate", 2, "c2"),
        _record("g3", "text-generation", "checkpoint_generate", 3, "c3"),
        _record("g4", "text-generation", "checkpoint_generate", 4, "c4"),
        _record("complete", "text-generation", "complete_generate", 4, "c4"),
    ]
    selection = {
        "schema_version": "generate-materiality-selection-v1",
        "decisions": [
            _decision("g2", "g1"),
            _decision("complete", "g2"),
        ],
    }

    curated, report = curate_records(rows, selection)

    assert [row["instance_id"] for row in curated] == ["g1", "g2", "complete"]
    assert report["excluded"] == [
        {"instance_id": "g3", "reason": "no_passing_materiality_decision"},
        {
            "instance_id": "g4",
            "reason": "duplicate_destination_with_complete_generate",
        },
    ]


def test_complete_without_materiality_replaces_initial_instead_of_duplicating_it():
    rows = [
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("complete", "text-generation", "complete_generate", 2, "c2"),
    ]

    curated, report = curate_records(
        rows,
        {"schema_version": "generate-materiality-selection-v1", "decisions": []},
    )

    assert [row["instance_id"] for row in curated] == ["complete"]
    assert report["excluded"] == [
        {"instance_id": "g1", "reason": "complete_not_proven_significant_vs_initial"}
    ]


def test_partial_trajectory_keeps_only_initial_generate_without_materiality():
    rows = [
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("g2", "text-generation", "checkpoint_generate", 2, "c2"),
    ]

    curated, report = curate_records(
        rows,
        {"schema_version": "generate-materiality-selection-v1", "decisions": []},
    )

    assert [row["instance_id"] for row in curated] == ["g1"]
    assert report["excluded"] == [
        {"instance_id": "g2", "reason": "no_passing_materiality_decision"}
    ]


def test_materiality_decision_must_be_semantically_reviewed():
    rows = [
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("complete", "text-generation", "complete_generate", 2, "c2"),
    ]
    invalid = _decision("complete", "g1")
    invalid["reviewer"] = {"kind": "code_threshold", "name": "heuristic"}

    with pytest.raises(ValueError, match="semantic_review or human"):
        curate_records(
            rows,
            {
                "schema_version": "generate-materiality-selection-v1",
                "decisions": [invalid],
            },
        )


def test_materiality_decision_requires_nonempty_semantic_evidence():
    rows = [
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("complete", "text-generation", "complete_generate", 2, "c2"),
    ]
    invalid = _decision("complete", "g1")
    invalid["evidence"] = []

    with pytest.raises(ValueError, match="evidence must be non-empty"):
        curate_records(
            rows,
            {
                "schema_version": "generate-materiality-selection-v1",
                "decisions": [invalid],
            },
        )


def test_materiality_decision_cannot_target_edit_record():
    rows = [_record("edit", "text-editing", "canonical_edit", 2, "c2")]

    with pytest.raises(ValueError, match="non-Generate"):
        curate_records(
            rows,
            {
                "schema_version": "generate-materiality-selection-v1",
                "decisions": [_decision("edit", "g1")],
            },
        )


def test_curated_release_is_immutable_and_writes_family_views(tmp_path: Path):
    input_path = tmp_path / "records.jsonl"
    rows = [
        _record("edit", "text-editing", "canonical_edit", 2, "c2"),
        _record("g1", "text-generation", "checkpoint_generate", 1, "c1"),
        _record("complete", "text-generation", "complete_generate", 2, "c2"),
    ]
    input_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    selection = {
        "schema_version": "generate-materiality-selection-v1",
        "decisions": [_decision("complete", "g1")],
    }
    output_dir = tmp_path / "release"

    manifest = write_curated_release(input_path, output_dir, selection)

    assert manifest["counts"]["text-generation"] == 2
    assert (output_dir / "records.jsonl").is_file()
    assert (output_dir / "generate.jsonl").is_file()
    assert (output_dir / "edit.jsonl").is_file()
    assert not (output_dir / "repair.jsonl").exists()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_curated_release(input_path, output_dir, selection)
