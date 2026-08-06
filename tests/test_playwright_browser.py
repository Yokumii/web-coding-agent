from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.playwright_browser import launch_chromium


@pytest.mark.anyio
async def test_falls_back_to_system_chrome_when_bundle_missing(tmp_path: Path):
    calls = []

    class BrowserType:
        executable_path = str(tmp_path / "missing")

        async def launch(self, **kwargs):
            calls.append(kwargs)
            return "browser"

    result = await launch_chromium(SimpleNamespace(chromium=BrowserType()))
    assert result == "browser"
    assert calls == [{"channel": "chrome", "headless": True}]
