from __future__ import annotations

from pathlib import Path

from src.agents.visual_capture import _build_visual_capture_prompt


def test_visual_capture_prompt_forbids_bash_and_absolute_paths(tmp_path: Path):
    prompt = _build_visual_capture_prompt(
        round_num=1,
        app_url="http://127.0.0.1:5173",
        workdir=tmp_path,
    )

    assert "Do not call Bash." in prompt
    assert "Do not create directories." in prompt
    assert "Use only relative paths" in prompt
    assert "Write the manifest with the Write tool only." in prompt
