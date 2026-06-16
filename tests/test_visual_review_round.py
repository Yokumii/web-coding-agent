import asyncio
from pathlib import Path
from typing import Any

from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.visual_review_round import VisualReviewRound


def _placeholder_grades() -> dict[str, Any]:
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


def test_visual_review_round_builds_manifest_from_screenshot_files(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "visual_round_2_home.png").write_bytes(b"PNGFAKE")
    (file_comm.dir / "visual_round_2_bottom.png").write_bytes(b"PNGFAKE")

    review_round = VisualReviewRound(
        config=HarnessConfig(),
        file_comm=file_comm,
        workdir=tmp_path,
        round_num=2,
        sprint_num=1,
        sprint_context={"title": "Sprint", "goal": "Goal"},
    )

    assert review_round.manifest() == {
        "round": 2,
        "app_url": "",
        "screenshots": [
            ".harness/visual_round_2_bottom.png",
            ".harness/visual_round_2_home.png",
        ],
        "notes": "",
    }


def test_visual_review_round_prefers_written_manifest(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_visual_manifest(
        1,
        {
            "round": 1,
            "app_url": "http://localhost",
            "screenshots": [".harness/custom.png"],
            "notes": "captured",
        },
    )

    review_round = VisualReviewRound(
        config=HarnessConfig(),
        file_comm=file_comm,
        workdir=tmp_path,
        round_num=1,
        sprint_num=1,
        sprint_context={"title": "Sprint", "goal": "Goal"},
    )

    assert review_round.manifest() == {
        "round": 1,
        "app_url": "http://localhost",
        "screenshots": [".harness/custom.png"],
        "notes": "captured",
    }


def test_visual_review_round_applies_successful_review(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "visual_round_1_home.png").write_bytes(b"PNGFAKE")
    review_calls: list[list[str]] = []

    async def fake_reviewer(**kwargs):
        review_calls.append(kwargs["screenshot_paths"])
        return {"raw": "review"}, {"total_cost_usd": 0.2}

    def fake_normalizer(review, screenshot_paths):
        assert review == {"raw": "review"}
        return {
            "phase_result": "pass",
            "appearance_review": {
                "render_stability": 5,
                "content_relevance": 5,
                "layout_harmony": 5,
                "modernness_memorability": 5,
                "token_adherence": 5,
                "notes": "good",
                "screenshots": screenshot_paths,
            },
            "criteria_scores": {
                "design_quality": {"score": 8.0, "notes": "good"},
                "originality": {"score": 7.0, "notes": "good"},
                "craft": {"score": 8.0, "notes": "good"},
            },
        }

    review_round = VisualReviewRound(
        config=HarnessConfig(),
        file_comm=file_comm,
        workdir=tmp_path,
        round_num=1,
        sprint_num=1,
        sprint_context={"title": "Sprint", "goal": "Goal"},
    )

    merged, stats = asyncio.run(
        review_round.apply(
            grades=_placeholder_grades(),
            reviewer=fake_reviewer,
            normalizer=fake_normalizer,
        )
    )

    assert review_calls == [[".harness/visual_round_1_home.png"]]
    assert stats == {"total_cost_usd": 0.2}
    assert merged["phase_results"]["appearance"] == "pass"
    assert merged["criteria"]["design_quality"]["score"] == 8.0
    assert merged["appearance_review"]["screenshots"] == [
        ".harness/visual_round_1_home.png"
    ]
