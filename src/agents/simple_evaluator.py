from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts


def build_simple_grades(*, round_num: int, sprint_num: int, title: str,
                        body_text: str, page_errors: list[str]) -> dict[str, Any]:
    passed = bool(title.strip() and len(body_text.strip()) >= 20 and not page_errors)
    score = 7.0 if passed else 0.0
    note = "Deterministic render gate passed." if passed else "Deterministic render gate failed: " + ("; ".join(page_errors) or "page content is empty")
    return {
        "round": round_num, "sprint": sprint_num,
        "criteria": {name: {"score": score, "passed": passed, "notes": note} for name in ("design_quality", "functionality", "originality", "craft")},
        "overall_passed": passed, "sprint_passed": passed, "regression_passed": passed,
        "mode_recommendation": "generate_next_sprint" if passed else "repair",
        "phase_results": {"render_gate": "pass" if passed else "fail", "ui_functionality": "skipped", "appearance": "pass" if passed else "fail", "source_inspection": "skipped"},
        "appearance_review": {"screenshots": [], "render_stability": score, "content_relevance": score, "layout_harmony": score, "modernness_memorability": score, "token_adherence": score, "notes": "Simple evaluator: desktop/mobile render and runtime-error gate only."},
        "bugs_found": page_errors, "regressions_found": [], "missing_features": [],
        "repair_instructions": ["Fix browser runtime errors and restore a non-empty render."] if not passed else [],
    }


async def run_simple_evaluator(*, file_comm: FileComm, workdir: Path, round_num: int,
                               sprint_num: int, app_url: str):
    from playwright.async_api import async_playwright
    from src.agents.sdk_runner import AgentRunStats
    started = time.monotonic()
    errors: list[str] = []
    artifacts = RoundArtifacts(file_comm, round_num)
    refs = [artifacts.visual_capture_refs[0], f".harness/visual_round_{round_num}_mobile.png"]
    paths = [workdir / ref for ref in refs]
    for path in paths: path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        from src.utils.playwright_browser import launch_chromium
        browser = await launch_chromium(playwright, headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            response = await page.goto(app_url, wait_until="networkidle", timeout=30_000)
            if response is None or not response.ok: errors.append(f"HTTP render failed: {response.status if response else 'no response'}")
            title, body_text = await page.title(), await page.locator("body").inner_text()
            await page.screenshot(path=str(paths[0]), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.reload(wait_until="networkidle", timeout=30_000)
            await page.screenshot(path=str(paths[1]), full_page=True)
        finally: await browser.close()
    grades = build_simple_grades(round_num=round_num, sprint_num=sprint_num, title=title, body_text=body_text, page_errors=errors)
    grades["appearance_review"]["screenshots"] = refs
    file_comm.write_grades(round_num, grades)
    file_comm.write_visual_manifest(round_num, {"round": round_num, "app_url": app_url, "screenshots": refs, "notes": "Simple evaluator desktop and mobile render captures."})
    duration = int((time.monotonic() - started) * 1000)
    return grades["overall_passed"], grades, AgentRunStats(0.0, duration, 0, {}, {}, {})
