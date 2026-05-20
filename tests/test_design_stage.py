from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.design_stage import run_design_stage
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_design_stage_records_text_only_fallback_when_assets_are_missing(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    result = await run_design_stage(
        HarnessConfig(design_mode="image-first", design_image_api_key=""),
        file_comm,
        tmp_path,
    )

    assert result.metadata == {
        "requested_design_mode": "image-first",
        "design_mode": "text_only_fallback",
        "design_status": "fallback_text_only",
        "approved_concept_path": None,
        "background_ui_path": None,
    }
    brief = file_comm.read_design_brief()
    assert brief["requested_mode"] == "image-first"
    assert brief["visual_strategy"] == "text_only_fallback"
    assert brief["reference_files"] == {}
    assert brief["fallback_reason"] == "image_assets_unavailable"
    assert file_comm.read_layout_contract()["asset_fit"] == {}
    assert file_comm.read_asset_manifest()["assets"] == []


@pytest.mark.anyio
async def test_design_stage_adopts_preseeded_image_assets(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.design_dir.mkdir(parents=True)
    (file_comm.design_dir / "approved_concept.png").write_bytes(b"concept")
    (file_comm.design_dir / "background_ui.png").write_bytes(b"background")

    result = await run_design_stage(
        HarnessConfig(design_mode="image-first", design_image_api_key=""),
        file_comm,
        tmp_path,
    )

    assert result.metadata == {
        "requested_design_mode": "image-first",
        "design_mode": "image_backed_ui",
        "design_status": "accepted",
        "approved_concept_path": ".harness/design/approved_concept.png",
        "background_ui_path": ".harness/design/background_ui.png",
    }
    assert file_comm.read_design_brief()["reference_files"] == {
        "approved_concept": ".harness/design/approved_concept.png",
        "background_ui": ".harness/design/background_ui.png",
    }
    assert file_comm.read_layout_contract()["asset_fit"] == {
        "background_ui": "cover_desktop_contain_mobile"
    }


@pytest.mark.anyio
async def test_design_stage_generates_missing_assets_when_image_api_is_configured(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_spec("# Example")
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial",
            "color": {"bg": "#111"},
            "typography": {"display": "Sans"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"fast": 100},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": [],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    calls: list[dict] = []

    async def fake_generate_image(**kwargs):
        calls.append(kwargs)
        kwargs["output_path"].write_bytes(b"png")

    monkeypatch.setattr("src.agents.design_stage.generate_image", fake_generate_image)

    result = await run_design_stage(
        HarnessConfig(
            design_mode="image-first",
            design_image_api_key="test-key",
        ),
        file_comm,
        tmp_path,
    )

    assert result.metadata["design_mode"] == "image_backed_ui"
    assert [call["output_path"].name for call in calls] == [
        "approved_concept.png",
        "background_ui.png",
    ]
    assert calls[0]["reference_images"] is None
    assert calls[1]["reference_images"] == [
        file_comm.design_dir / "approved_concept.png"
    ]
