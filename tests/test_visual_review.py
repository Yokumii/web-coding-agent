"""Tests for the dedicated visual review degraded paths.

The original failure mode: when the vision scorer raised or returned no
screenshots, the harness silently kept evaluator-written placeholder
grades, so the run could "pass" without any visual scoring at all. The
behaviour this test suite locks in is:

    vision unavailable  ->  overall_passed=False
                            mode_recommendation="repair"
                            sprint_passed=False
                            criteria.{design_quality,originality,craft}.passed=False
                            phase_results.appearance="fail"
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agents.visual_review import apply_dedicated_visual_review
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm


def _placeholder_grades() -> dict:
    return {
        "round": 1,
        "sprint": 1,
        "sprint_passed": True,
        "overall_passed": True,
        "mode_recommendation": "complete",
        "phase_results": {
            "render_gate": "pass",
            "ui_functionality": "pass",
            "appearance": "pass",
            "source_inspection": "pass",
        },
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True, "notes": "placeholder"},
            "functionality": {"score": 7.0, "passed": True, "notes": "ok"},
            "originality": {"score": 6.0, "passed": True, "notes": "placeholder"},
            "craft": {"score": 7.0, "passed": True, "notes": "placeholder"},
        },
    }


def _make_file_comm_with_screenshot(workdir: Path) -> FileComm:
    harness_dir = workdir / ".harness"
    file_comm = FileComm(harness_dir)
    (harness_dir / "visual_round_1_home.png").write_bytes(b"PNGFAKE")
    return file_comm


def _assert_visual_failure_recorded(merged: dict) -> None:
    assert merged["overall_passed"] is False
    assert merged["mode_recommendation"] == "repair"
    assert merged["sprint_passed"] is False
    for name in ("design_quality", "originality", "craft"):
        assert merged["criteria"][name]["passed"] is False
        assert merged["criteria"][name]["score"] == 0.0
    # functionality is owned by the functional evaluator, not vision; do not touch it
    assert merged["criteria"]["functionality"]["passed"] is True
    assert merged["phase_results"]["appearance"] == "fail"


def test_apply_dedicated_visual_review_marks_failure_when_scorer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_comm = _make_file_comm_with_screenshot(tmp_path)
    grades = _placeholder_grades()

    async def fake_review(**_kwargs):
        raise RuntimeError("vision scorer HTTP 503: upstream down")

    monkeypatch.setattr(
        "src.agents.visual_review.run_visual_appearance_review", fake_review
    )

    config = HarnessConfig(
        evaluator_vision_model="x",
        evaluator_vision_api_key="x",
    )

    merged, stats = asyncio.run(
        apply_dedicated_visual_review(
            config=config,
            file_comm=file_comm,
            workdir=tmp_path,
            round_num=1,
            sprint_num=1,
            sprint_context={"title": "T", "goal": "g"},
            grades=grades,
            manifest=None,
        )
    )

    assert stats is None
    _assert_visual_failure_recorded(merged)
    assert merged["evaluation_infrastructure_failure"]["phase"] == "visual_review"
    # The original placeholder grades dict must not be mutated in place.
    assert grades["overall_passed"] is True


def test_apply_dedicated_visual_review_marks_failure_when_no_screenshots(
    tmp_path: Path,
) -> None:
    harness_dir = tmp_path / ".harness"
    file_comm = FileComm(harness_dir)
    # NB: no PNGs written, no manifest, no appearance_review.screenshots
    grades = _placeholder_grades()

    config = HarnessConfig(
        evaluator_vision_model="x",
        evaluator_vision_api_key="x",
    )

    merged, stats = asyncio.run(
        apply_dedicated_visual_review(
            config=config,
            file_comm=file_comm,
            workdir=tmp_path,
            round_num=1,
            sprint_num=1,
            sprint_context={"title": "T", "goal": "g"},
            grades=grades,
            manifest=None,
        )
    )

    assert stats is None
    _assert_visual_failure_recorded(merged)
