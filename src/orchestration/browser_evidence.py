"""Execute planner-authored browser contracts before LLM evaluation.

This layer does no semantic grading: it faithfully records whether the planned
actions ran in a real Playwright page.  The evaluator remains responsible for
interpreting the observed result and describing a repair.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _is_invalid_test_contract_error(action: str, exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return action == "evaluate" and "SyntaxError" in message


def _action_settle_ms(step: dict[str, Any], action: str) -> int:
    """Return an explicit inter-action settling delay for state-producing actions."""
    if action == "evaluate":
        return 0
    value = step.get("settle_ms", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5_000:
        raise ValueError("settle_ms must be an integer from 0 to 5000")
    return value


def _same_origin_route_url(app_url: str, route: str) -> str:
    """Resolve a planner route without allowing navigation off the app origin."""
    base = urlsplit(app_url)
    target = urlsplit(route)
    segments = target.path.replace("\\", "/").split("/")
    if (
        base.scheme not in {"http", "https"}
        or not base.netloc
        or not route.startswith("/")
        or route.startswith("//")
        or "\\" in route
        or target.scheme
        or target.netloc
        or target.query
        or target.fragment
        or any(segment in {".", ".."} for segment in segments)
    ):
        raise ValueError(f"unsafe browser route: {route!r}")
    return urlunsplit((base.scheme, base.netloc, target.path or "/", "", target.fragment))


async def collect_browser_evidence(
    *, app_url: str, checks: list[dict[str, Any]], output_path: Path, headless: bool,
    fail_fast: bool = False, action_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium

    records: list[dict[str, Any]] = []
    # Legacy plans and narrow unit-level harness runs may have no executable
    # actions.  There is nothing to observe, so do not open a real browser (or
    # accidentally turn an intentionally stubbed app stack into a connection
    # failure).  New planner output is validated separately and must contain
    # one final assertion per check.
    if not checks:
        payload = {"app_url": app_url, "checks": records}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 812})
            # A contract miss is evidence about this one UI check, not a reason
            # to burn the whole evaluation budget waiting on Playwright's 30s
            # default for every absent selector.
            page.set_default_timeout(action_timeout_ms)
            # Preserve state between adjacent checks on one route, but perform
            # a deliberate navigation when a multi-page contract changes route.
            active_route: str | None = None
            for check in checks:
                steps = check.get("actions") if isinstance(check, dict) else []
                route = str(check.get("route", "/"))
                item: dict[str, Any] = {
                    "check_id": check.get("id"),
                    "route": route,
                    "steps": [],
                }
                try:
                    route_url = _same_origin_route_url(app_url, route)
                    if route != active_route:
                        await page.goto(
                            route_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        await page.wait_for_timeout(150)
                        active_route = route
                    item["url"] = page.url
                except Exception as exc:
                    item.update(
                        {
                            "status": "invalid_test_contract",
                            "navigation_error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    records.append(item)
                    if fail_fast:
                        break
                    continue
                if not isinstance(steps, list) or not steps:
                    item["status"] = "no_action_contract"
                    records.append(item)
                    continue
                try:
                    for step in steps:
                        result: dict[str, Any] = {"action": step.get("action")}
                        try:
                            action = str(step.get("action"))
                            if action == "set_viewport":
                                await page.set_viewport_size({"width": int(step["width"]), "height": int(step["height"])})
                                result["output"] = {"width": int(step["width"]), "height": int(step["height"])}
                            elif action == "click":
                                await page.click(str(step["selector"]))
                                result["output"] = "clicked"
                            elif action == "key_press":
                                selector = step.get("selector")
                                if selector:
                                    await page.focus(str(selector))
                                key = str(step["key"])
                                known_keys = {"Tab", "Enter", "Escape", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace"}
                                if key not in known_keys:
                                    await page.keyboard.insert_text(key)
                                    result["output"] = f"typed {key}"
                                else:
                                    for _ in range(int(step.get("count", 1))):
                                        await page.keyboard.press(key)
                                    result["output"] = f"pressed {key} x{int(step.get('count', 1))}"
                            elif action == "fill":
                                await page.fill(str(step["selector"]), str(step["value"]))
                                result["output"] = "filled"
                            elif action == "select_option":
                                await page.select_option(str(step["selector"]), str(step["value"]))
                                result["output"] = "selected"
                            elif action == "assert_form_valid":
                                result["output"] = await page.locator(str(step["selector"])).evaluate(
                                    "form => form.checkValidity()"
                                )
                                result["test_precondition"] = True
                            elif action == "scroll":
                                requested_y = int(step.get("y", 0))
                                await page.evaluate("y => window.scrollTo(0, y)", requested_y)
                                result["output"] = await page.evaluate("window.scrollY")
                                # A scroll-triggered feature cannot be judged
                                # on a page that has no scrollable distance.
                                # This is a malformed test/seed combination,
                                # not evidence that the implementation needs a
                                # repair which would artificially force the UI
                                # visible.
                                if requested_y > 0 and result["output"] == 0:
                                    result["test_precondition"] = True
                            elif action == "evaluate":
                            # Input handlers in otherwise static frontends
                            # commonly debounce rendering.  This bounded pause
                            # is part of executing a user interaction, not an
                            # LLM judgement; without it a correct UI can be
                            # mislabeled as a repair task solely on timing.
                                await page.wait_for_timeout(int(step.get("settle_ms", 1_000)))
                                result["output"] = await page.evaluate(str(step["expression"]))
                            else:
                                raise ValueError(f"unsupported action: {action}")
                            settle_ms = _action_settle_ms(step, action)
                            if settle_ms:
                                await page.wait_for_timeout(settle_ms)
                            # `evaluate` is the assertion operation in a browser
                            # contract. A false expression is a reproduced UI failure.
                            result["ok"] = (
                                False if action == "scroll" and result.get("test_precondition")
                                else bool(result["output"]) if action in {"evaluate", "assert_form_valid"}
                                else True
                            )
                        except Exception as exc:
                            result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                            if _is_invalid_test_contract_error(action, exc):
                                result["test_precondition"] = True
                        item["steps"].append(result)
                finally:
                    pass
                failed_precondition = any(
                    step.get("test_precondition") and not step.get("ok")
                    for step in item["steps"]
                )
                item["status"] = (
                    "invalid_test_contract" if failed_precondition
                    else "ok" if all(step.get("ok") for step in item["steps"])
                    else "action_failed"
                )
                records.append(item)
                if fail_fast and item["status"] != "ok":
                    break
        finally:
            await page.close()
            await browser.close()
    payload = {"app_url": app_url, "checks": records}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload
