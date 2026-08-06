from __future__ import annotations

from pathlib import Path
from typing import Any


async def launch_chromium(playwright: Any, *, headless: bool = True):
    """Launch bundled Chromium when installed, otherwise use system Chrome."""
    executable = Path(playwright.chromium.executable_path)
    if executable.is_file():
        return await playwright.chromium.launch(headless=headless)
    return await playwright.chromium.launch(channel="chrome", headless=headless)
