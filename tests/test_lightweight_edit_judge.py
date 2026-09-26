from __future__ import annotations

import base64
from types import SimpleNamespace
from aiohttp import web
import pytest

from src.agents.lightweight_edit_judge import (
    _capture_page_screenshot,
    _normalize_judgement,
    build_lightweight_grades,
    capture_runtime_snapshot,
)
from src.orchestration.phases import _select_generator_mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _serve(handler):
    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/"


@pytest.mark.anyio
async def test_runtime_ignores_noncritical_image_404(tmp_path):
    async def handler(_request):
        return web.Response(
            text='<main>ready</main><img src="/missing.png">', content_type="text/html"
        )

    runner, url = await _serve(handler)
    try:
        result = await capture_runtime_snapshot(
            app_url=url, screenshot_path=tmp_path / "after.png", headless=True, retries=0
        )
    finally:
        await runner.cleanup()

    assert result["status"] == "pass"
    assert result["fatal_runtime_errors"] == []


@pytest.mark.anyio
async def test_runtime_keeps_failed_script_fatal(tmp_path):
    async def handler(_request):
        return web.Response(
            text='<main>ready</main><script src="/missing.js"></script>', content_type="text/html"
        )

    runner, url = await _serve(handler)
    try:
        result = await capture_runtime_snapshot(
            app_url=url, screenshot_path=tmp_path / "after.png", headless=True, retries=0
        )
    finally:
        await runner.cleanup()

    assert result["status"] == "issue"
    assert any("loading script" in error for error in result["fatal_runtime_errors"])


@pytest.mark.anyio
async def test_screenshot_uses_cdp_when_playwright_capture_stalls(tmp_path):
    png = b"fallback screenshot"

    class Session:
        async def send(self, method, params):
            assert method == "Page.captureScreenshot"
            assert params["captureBeyondViewport"] is False
            return {"data": base64.b64encode(png).decode("ascii")}

    class Context:
        async def new_cdp_session(self, page):
            return Session()

    class Page:
        context = Context()

        async def screenshot(self, **kwargs):
            raise TimeoutError("capture stalled")

    output = tmp_path / "fallback.png"
    method = await _capture_page_screenshot(Page(), output, timeout_ms=100)

    assert method == "cdp_fallback"
    assert output.read_bytes() == png


@pytest.mark.parametrize("recommendation", ["repair", "discard"])
def test_failed_judge_result_always_selects_repair(recommendation):
    ctx = SimpleNamespace(file_comm=SimpleNamespace(read_grades=lambda _round: {
        "overall_passed": False,
        "mode_recommendation": recommendation,
        "sprint": 1,
    }))

    assert _select_generator_mode(ctx, round_num=2, sprint_num=1, resume_state=None) == "repair"


def test_unexplained_issue_flag_does_not_create_repair():
    result = _normalize_judgement({
        "instruction_targeting": "issue",
        "feature_integrity": "pass",
        "style_conformance": "pass",
        "issues": [],
    }, {"status": "pass"})

    assert result["instruction_targeting"] == "pass"


def test_all_major_issues_are_combined_into_one_repair_instruction(tmp_path):
    after = tmp_path / "after.png"
    after.write_bytes(b"png")
    grades = build_lightweight_grades(
        round_num=1,
        sprint_num=1,
        runtime={"status": "pass"},
        judgement={
            "instruction_targeting": "issue",
            "feature_integrity": "issue",
            "style_conformance": "pass",
            "issues": [
                {"dimension": "instruction_targeting", "description": "Core control is static."},
                {"dimension": "feature_integrity", "description": "Existing content was removed."},
            ],
        },
        before=None,
        after=after,
        diff="patch",
    )

    assert len(grades["repair_instructions"]) == 1
    assert "Core control is static" in grades["repair_instructions"][0]
    assert "Existing content was removed" in grades["repair_instructions"][0]
