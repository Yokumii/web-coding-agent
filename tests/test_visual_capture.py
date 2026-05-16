from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.visual_capture import build_visual_capture_requirements, run_visual_capture
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_visual_capture_requirements_keep_relative_artifact_contract():
    prompt = "\n".join(
        build_visual_capture_requirements(
            round_num=1,
            app_url="http://127.0.0.1:5173",
        )
    )

    assert "Only include screenshots that were actually created." in prompt
    assert "Use only relative paths" in prompt
    assert "Write the manifest with the Write tool only." in prompt
    assert "downstream VLM review" in prompt


@pytest.mark.anyio
async def test_run_visual_capture_reads_manifest_when_present(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    manifest = {
        "round": 1,
        "app_url": "http://127.0.0.1:5173",
        "screenshots": [".harness/visual_round_1_home.png"],
        "notes": "top view",
    }
    file_comm.write_visual_manifest(1, manifest)

    loaded, stats = await run_visual_capture(
        config=None,
        file_comm=file_comm,
        workdir=tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:5173",
    )

    assert loaded == manifest
    assert stats.cost_usd == 0.0


@pytest.mark.anyio
async def test_run_visual_capture_falls_back_to_saved_pngs(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "visual_round_2_home.png").write_text("png")
    (file_comm.dir / "visual_round_2_mid.png").write_text("png")

    loaded, stats = await run_visual_capture(
        config=None,
        file_comm=file_comm,
        workdir=tmp_path,
        round_num=2,
        app_url="http://127.0.0.1:5173",
    )

    assert loaded == {
        "round": 2,
        "app_url": "",
        "screenshots": [
            ".harness/visual_round_2_home.png",
            ".harness/visual_round_2_mid.png",
        ],
        "notes": "",
    }
    assert json.loads(json.dumps(loaded)) == loaded
    assert stats.cost_usd == 0.0
