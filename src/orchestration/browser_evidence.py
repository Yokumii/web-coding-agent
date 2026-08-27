"""Execute planner-authored browser contracts before LLM evaluation.

This layer performs bounded deterministic grading over real Playwright evidence.
Reproduced failures can enter Repair directly without a paid semantic judge.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.orchestration.ui_action_contracts import (
    ASSERTION_UI_ACTIONS,
    ActionContractError,
    TYPED_ASSERTION_ACTIONS,
    validate_ui_action,
    validate_ui_action_sequence,
)


def _is_invalid_test_contract_error(action: str, exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return isinstance(exc, ActionContractError) or (
        action == "evaluate" and "SyntaxError" in message
    )


def _action_settle_ms(step: dict[str, Any], action: str) -> int:
    """Return an explicit inter-action settling delay for state-producing actions."""
    if action == "evaluate":
        return 0
    value = step.get("settle_ms", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5_000:
        raise ValueError("settle_ms must be an integer from 0 to 5000")
    return value


def _matches(actual: Any, expected: Any, mode: str = "exact") -> bool:
    actual_text = "" if actual is None else str(actual)
    expected_text = "" if expected is None else str(expected)
    return expected_text in actual_text if mode == "contains" else actual_text == expected_text


def _evidence_route(actions: list[dict[str, Any]]) -> list[str]:
    routes: list[str] = ["real_browser"]
    kinds = {str(item.get("action", "")) for item in actions if isinstance(item, dict)}
    if kinds & {"assert_aria", "assert_focus"}:
        routes.append("ax_semantics")
    if kinds & {"assert_property", "assert_storage_value", "assert_url", "assert_value"}:
        routes.append("internal_state")
    if kinds & TYPED_ASSERTION_ACTIONS:
        routes.append("dom")
    return routes


def _aria_snapshot_root(snapshot: str) -> tuple[str | None, str | None]:
    """Parse the root role/name from Playwright's harness-owned ARIA snapshot."""
    first = next((line.strip() for line in snapshot.splitlines() if line.strip()), "")
    match = re.match(r'^-\s+([^\s:"]+)(?:\s+("(?:\\.|[^"])*"))?', first)
    if not match:
        return None, None
    name: str | None = None
    if match.group(2):
        try:
            name = str(json.loads(match.group(2)))
        except (TypeError, ValueError, json.JSONDecodeError):
            name = match.group(2).strip('"')
    return match.group(1), name


async def _execute_typed_assertion(
    *, page: Any, step: dict[str, Any], console_errors: list[str]
) -> tuple[bool, dict[str, Any]]:
    """Execute one bounded assertion without running planner-authored JavaScript."""
    action = str(step["action"])
    selector = str(step.get("selector", ""))
    locator = page.locator(selector) if selector else None
    expected = step.get("value")
    actual: Any
    aria_snapshot: str | None = None

    if action == "assert_visible":
        actual = await locator.is_visible()
        ok = actual is True
    elif action == "assert_hidden":
        actual = await locator.is_hidden()
        ok = actual is True
    elif action == "assert_text":
        actual = await locator.text_content()
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_value":
        actual = await locator.input_value()
        ok = _matches(actual, expected)
    elif action == "assert_count":
        actual = await locator.count()
        expected = int(step["count"])
        ok = actual == expected
    elif action == "assert_property":
        name = str(step["name"])
        actual = await locator.evaluate("(element, key) => element[key]", name)
        ok = actual == expected
    elif action == "assert_url":
        actual = urlsplit(page.url).path if str(expected).startswith("/") else page.url
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_attribute":
        actual = await locator.get_attribute(str(step["name"]))
        ok = _matches(actual, expected)
    elif action == "assert_aria":
        attribute = str(step["attribute"])
        if attribute in {"accessible_name", "role"}:
            aria_snapshot = await locator.aria_snapshot()
            role, accessible_name = _aria_snapshot_root(aria_snapshot)
            actual = accessible_name if attribute == "accessible_name" else role
        else:
            actual = await locator.get_attribute(attribute)
        ok = _matches(actual, str(expected).lower() if isinstance(expected, bool) else expected)
    elif action == "assert_focus":
        actual = await locator.evaluate("element => element === document.activeElement")
        ok = actual is True
    elif action == "assert_storage_value":
        storage = str(step["storage"])
        actual = await page.evaluate(
            "([kind, key]) => (kind === 'local' ? localStorage : sessionStorage).getItem(key)",
            [storage, str(step["key"])],
        )
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_no_console_errors":
        actual = list(console_errors)
        expected = []
        ok = not actual
    else:  # pragma: no cover - caller and validator keep this unreachable
        raise ActionContractError(f"unsupported typed assertion {action!r}")

    output = {"actual": actual, "expected": expected}
    if aria_snapshot is not None:
        output["aria_snapshot"] = aria_snapshot
    return ok, output


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
    # a bounded set of related assertions ending in a typed assertion.
    if not checks:
        payload = {"app_url": app_url, "checks": records}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        page = None
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 812})
            console_errors: list[str] = []
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            # A contract miss is evidence about this one UI check, not a reason
            # to burn the whole evaluation budget waiting on Playwright's 30s
            # default for every absent selector.
            page.set_default_timeout(action_timeout_ms)
            # Preserve state between adjacent checks on one route, but perform
            # a deliberate navigation when a multi-page contract changes route.
            active_route: str | None = None
            for check in checks:
                console_error_start = len(console_errors)
                steps = check.get("actions") if isinstance(check, dict) else []
                route = str(check.get("route", "/"))
                item: dict[str, Any] = {
                    "check_id": check.get("id"),
                    "route": route,
                    "steps": [],
                    "evidence_route": _evidence_route(steps if isinstance(steps, list) else []),
                }
                try:
                    route_url = _same_origin_route_url(app_url, route)
                    current_location = urlsplit(page.url) if page.url else None
                    target_location = urlsplit(route_url)
                    location_drifted = (
                        current_location is None
                        or current_location.path != target_location.path
                        or current_location.query != target_location.query
                        or current_location.fragment != target_location.fragment
                    )
                    if route != active_route or location_drifted:
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
                    validate_ui_action_sequence(steps)
                except ActionContractError as exc:
                    item.update({
                        "status": "invalid_test_contract",
                        "contract_error": f"{type(exc).__name__}: {exc}",
                    })
                    records.append(item)
                    if fail_fast:
                        break
                    continue
                try:
                    for step in steps:
                        result: dict[str, Any] = {"action": step.get("action")}
                        try:
                            validate_ui_action(step)
                            action = str(step.get("action"))
                            if action == "set_viewport":
                                await page.set_viewport_size({"width": int(step["width"]), "height": int(step["height"])})
                                result["output"] = {"width": int(step["width"]), "height": int(step["height"])}
                            elif action == "click":
                                await page.click(
                                    str(step["selector"]),
                                    button=str(step.get("button", "left")),
                                )
                                result["output"] = "clicked"
                            elif action == "hover":
                                await page.hover(str(step["selector"]))
                                result["output"] = "hovered"
                            elif action == "drag_and_drop":
                                await page.drag_and_drop(
                                    str(step["source_selector"]),
                                    str(step["target_selector"]),
                                )
                                result["output"] = "dragged"
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
                            elif action == "reload":
                                await page.reload(
                                    wait_until="domcontentloaded",
                                    timeout=15_000,
                                )
                                result["output"] = "reloaded"
                            elif action == "fill":
                                await page.fill(str(step["selector"]), str(step["value"]))
                                result["output"] = "filled"
                            elif action == "select_option":
                                await page.select_option(str(step["selector"]), str(step["value"]))
                                result["output"] = "selected"
                            elif action == "set_input_files":
                                payloads = [
                                    {
                                        "name": str(fixture["name"]),
                                        "mimeType": str(fixture["mime_type"]),
                                        "buffer": str(fixture["content"]).encode("utf-8"),
                                    }
                                    for fixture in step["files"]
                                ]
                                await page.set_input_files(str(step["selector"]), payloads)
                                result["output"] = {
                                    "uploaded": [str(item["name"]) for item in step["files"]]
                                }
                            elif action == "wait_for":
                                await page.locator(str(step["selector"])).wait_for(
                                    state=str(step.get("state", "visible")),
                                    timeout=int(step.get("timeout_ms", action_timeout_ms)),
                                )
                                result["output"] = str(step.get("state", "visible"))
                            elif action == "emulate_media":
                                options: dict[str, Any] = {}
                                if "media" in step:
                                    options["media"] = str(step["media"])
                                if "color_scheme" in step:
                                    options["color_scheme"] = str(step["color_scheme"])
                                await page.emulate_media(**options)
                                result["output"] = options
                            elif action == "assert_form_valid":
                                result["output"] = await page.locator(str(step["selector"])).evaluate(
                                    "form => form.checkValidity()"
                                )
                                result["test_precondition"] = True
                            elif action in TYPED_ASSERTION_ACTIONS:
                                assertion_ok, assertion_output = await _execute_typed_assertion(
                                    page=page,
                                    step=step,
                                    console_errors=console_errors[console_error_start:],
                                )
                                result["output"] = assertion_output
                                result["ok"] = assertion_ok
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
                            if "ok" not in result:
                                result["ok"] = (
                                    False if action == "scroll" and result.get("test_precondition")
                                    else bool(result["output"]) if action in ASSERTION_UI_ACTIONS | {"assert_form_valid"}
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
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=3)
                except (asyncio.TimeoutError, Exception):
                    pass
            try:
                await asyncio.wait_for(browser.close(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
    payload = {"app_url": app_url, "checks": records}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload
