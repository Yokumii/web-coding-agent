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
    HARNESS_ASSERTION_ACTIONS,
    TYPED_ASSERTION_ACTIONS,
    validate_ui_action,
    validate_ui_action_sequence,
)


# Read the displayed control state, not stale default markup. The wait and the
# recorded result must use the same semantics (especially for textarea.value).
_ASSERTION_TEXT_READER = """element => {
  if (element.tagName === 'TEXTAREA' || (element.tagName === 'INPUT' &&
      !['checkbox', 'radio', 'file', 'hidden', 'image'].includes(element.type))) {
    return element.value;
  }
  if (element.tagName === 'SELECT') {
    return [...element.selectedOptions].map(option => option.label).join('\\n');
  }
  return element.textContent;
}"""
BROWSER_EVIDENCE_POLICY_VERSION = "live_values_keyboard_chords_v4"
_ASSERTION_TEXT_VALUES = "(nodes, mode) => { const readText = " + _ASSERTION_TEXT_READER + ";" + """
  // A container already includes its descendant text. Independent matching
  // regions still each need to satisfy the assertion.
  const targets = mode === 'contains'
    ? nodes.filter(node => !nodes.some(other => other !== node && other.contains(node)))
    : nodes;
  return targets.map(readText);
}"""


def _is_invalid_test_contract_error(action: str, exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return isinstance(exc, ActionContractError) or 'strict mode violation' in message or (
        action == "evaluate" and "SyntaxError" in message
    )


def _is_nonblocking_react_warning(message: str) -> bool:
    # React development diagnostics use console.error even for DOM prop warnings.
    # Never apply this classification to pageerror exceptions or HTTP failures.
    return message.startswith((
        'Warning: Received ', 'Warning: Invalid DOM property ',
        'Warning: React does not recognize the ',
        'Warning: Unknown event handler property ',
        'Warning: Each child in a list should have a unique "key" prop',
        'Warning: validateDOMNesting(',
    ))


def _action_settle_ms(step: dict[str, Any], action: str) -> int:
    """Return an explicit inter-action settling delay for state-producing actions."""
    if action == "evaluate":
        return 0
    value = step.get("settle_ms", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5_000:
        raise ValueError("settle_ms must be an integer from 0 to 5000")
    return value


def _matches(actual: Any, expected: Any, mode: str = "exact") -> bool:
    if mode == "nonempty":
        return actual is not None and bool(str(actual).strip())
    actual_text = "" if actual is None else str(actual)
    expected_text = "" if expected is None else str(expected)
    return expected_text in actual_text if mode == "contains" else actual_text == expected_text


def _evidence_route(actions: list[dict[str, Any]]) -> list[str]:
    routes: list[str] = ["real_browser"]
    kinds = {str(item.get("action", "")) for item in actions if isinstance(item, dict)}
    if kinds & {"assert_aria", "assert_focus"}:
        routes.append("ax_semantics")
    if kinds & {
        "assert_hash",
        "assert_property",
        "assert_scroll",
        "assert_storage_value",
        "assert_url",
        "assert_value",
    }:
        routes.append("internal_state")
    if "set_hash" in kinds:
        routes.append("internal_state")
    if "assert_computed_style" in kinds:
        routes.append("rendered_style")
    if kinds & HARNESS_ASSERTION_ACTIONS:
        routes.extend(["layout_geometry", "webcompass_risk_audit"])
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


async def _visibility_diagnostic(page: Any, selector: str) -> dict[str, Any]:
    """Explain a hidden/missing target through bounded DOM/computed-style evidence."""
    locator = page.locator(selector)
    count = await locator.count()
    if count == 0:
        base_selector = re.sub(
            r"\[class\s*~=\s*(['\"])[^'\"]+\1\]\s*$", "", selector,
            flags=re.IGNORECASE,
        ).strip()
        base_count = (
            await page.locator(base_selector).count()
            if base_selector and base_selector != selector
            else 0
        )
        return {
            "matched_count": 0,
            **(
                {"base_selector": base_selector, "base_matched_count": base_count}
                if base_selector != selector
                else {}
            ),
        }
    return await locator.first.evaluate(
        r"""(element, selector) => {
          const describe = node => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            const className = typeof node.className === 'string'
              ? node.className.trim().split(/\s+/).filter(Boolean)[0] || ''
              : '';
            const label = node.id ? `#${node.id}`
              : node.getAttribute('data-testid')
                ? `[data-testid="${node.getAttribute('data-testid')}"]`
                : `${node.tagName.toLowerCase()}${className ? `.${className}` : ''}`;
            const hiddenReasons = [];
            if (node.hidden) hiddenReasons.push('hidden-attribute');
            if (node.getAttribute('aria-hidden') === 'true') hiddenReasons.push('aria-hidden=true');
            if (style.display === 'none') hiddenReasons.push('display=none');
            if (style.visibility === 'hidden') hiddenReasons.push('visibility=hidden');
            if (Number.parseFloat(style.opacity || '1') === 0) hiddenReasons.push('opacity=0');
            if (rect.width === 0 || rect.height === 0) hiddenReasons.push('zero-geometry');
            return {label, display: style.display, visibility: style.visibility,
              opacity: style.opacity, width: Math.round(rect.width), height: Math.round(rect.height),
              hidden_reasons: hiddenReasons};
          };
          const target = describe(element);
          const hiddenAncestors = [];
          let node = element.parentElement;
          while (node && hiddenAncestors.length < 6) {
            const state = describe(node);
            if (state.hidden_reasons.length) hiddenAncestors.push(state);
            node = node.parentElement;
          }
          return {matched_count: document.querySelectorAll(selector).length,
            target, hidden_ancestors: hiddenAncestors};
        }""",
        selector,
    )


async def _severe_contrast_issues(
    page: Any, selector_contracts: list[dict[str, Any]], route: str
) -> list[dict[str, Any]]:
    """Find nearly unreadable text in newly introduced visible surfaces.

    This deliberately uses a very low 1.5:1 threshold. It is a cheap failure
    gate for obvious theme-token mistakes, not a replacement for WCAG or a
    visual judge.
    """
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for contract in selector_contracts:
        if not isinstance(contract, dict) or str(contract.get("route") or "/") != route:
            continue
        selector = str(contract.get("selector") or "").strip()
        if not selector:
            continue
        findings = await page.locator(selector).evaluate_all(
            r"""roots => {
              const rgba = value => {
                const match = String(value || '').match(/[\d.]+/g);
                if (!match || match.length < 3) return null;
                return [Number(match[0]), Number(match[1]), Number(match[2]),
                  match.length > 3 ? Number(match[3]) : 1];
              };
              const luminance = color => {
                const channels = color.slice(0, 3).map(value => {
                  const unit = value / 255;
                  return unit <= 0.04045 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
                });
                return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
              };
              const background = element => {
                let node = element;
                while (node) {
                  const value = getComputedStyle(node).backgroundColor;
                  const parsed = rgba(value);
                  if (parsed && parsed[3] >= 0.95) return {value, parsed};
                  node = node.parentElement;
                }
                return {value: 'rgb(255, 255, 255)', parsed: [255, 255, 255, 1]};
              };
              const output = [];
              for (const root of roots) {
                for (const element of [root, ...root.querySelectorAll('*')]) {
                  const ownText = [...element.childNodes]
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => node.textContent || '').join(' ').replace(/\s+/g, ' ').trim();
                  if (!ownText) continue;
                  const style = getComputedStyle(element);
                  const rect = element.getBoundingClientRect();
                  if (style.display === 'none' || style.visibility === 'hidden' ||
                      Number(style.opacity) === 0 || rect.width === 0 || rect.height === 0) continue;
                  const foreground = rgba(style.color);
                  const bg = background(element);
                  if (!foreground || foreground[3] < 0.95) continue;
                  const a = luminance(foreground);
                  const b = luminance(bg.parsed);
                  const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
                  if (ratio < 1.5) output.push({
                    text: ownText.slice(0, 80),
                    foreground: style.color,
                    background: bg.value,
                    ratio: Math.round(ratio * 100) / 100,
                  });
                }
              }
              return output.slice(0, 12);
            }"""
        )
        for finding in findings:
            key = (selector, str(finding.get("foreground")), str(finding.get("background")))
            if key in seen:
                continue
            seen.add(key)
            issues.append({"selector": selector, **finding})
    return issues


async def _execute_typed_assertion(
    *,
    page: Any,
    step: dict[str, Any],
    console_errors: list[str],
    attribute_snapshots: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Execute one bounded assertion without running planner-authored JavaScript."""
    action = str(step["action"])
    selector = str(step.get("selector", ""))
    resolved_selector = await _resolve_behavioral_selector(page, selector, action)
    locator = page.locator(resolved_selector) if resolved_selector else None
    expected = step.get("value")
    actual: Any
    aria_snapshot: str | None = None

    if action == "assert_visible":
        # Assertions are eventual UI observations. A preceding click may start
        # an accepted animation/timer, so wait for the first match instead of
        # turning normal asynchronous rendering into a Repair trajectory.
        await locator.first.wait_for(state="visible")
        count = await locator.count()
        states = [await locator.nth(index).is_visible() for index in range(count)]
        actual = states[0] if count == 1 else states
        ok = count > 0 and all(states)
    elif action == "assert_hidden":
        count = await locator.count()
        # Playwright intentionally considers opacity:0 elements "visible". For
        # CSS-driven menus that is too narrow: opacity:0 together with
        # pointer-events:none is an interaction-hidden surface. Wait for the
        # transition and record this harness-owned computed-style/DOM semantic
        # instead of asking a screenshot model to infer it.
        if count:
            try:
                await page.wait_for_function(
                    """selector => [...document.querySelectorAll(selector)].every(element => {
                      const style = getComputedStyle(element);
                      const rect = element.getBoundingClientRect();
                      return element.hidden || element.getAttribute('aria-hidden') === 'true' ||
                        style.display === 'none' || style.visibility === 'hidden' ||
                        (Number.parseFloat(style.opacity || '1') === 0 &&
                         style.pointerEvents === 'none') ||
                        rect.width === 0 || rect.height === 0;
                    })""",
                    arg=selector,
                )
            except Exception:
                pass
        states = [
            await locator.nth(index).evaluate(
                """element => {
                  const style = getComputedStyle(element);
                  const rect = element.getBoundingClientRect();
                  return element.hidden || element.getAttribute('aria-hidden') === 'true' ||
                    style.display === 'none' || style.visibility === 'hidden' ||
                    (Number.parseFloat(style.opacity || '1') === 0 &&
                     style.pointerEvents === 'none') ||
                    rect.width === 0 || rect.height === 0;
                }"""
            )
            for index in range(count)
        ]
        actual = states[0] if count == 1 else states
        ok = count == 0 or all(states)
    elif action == "assert_in_view":
        await locator.first.wait_for(state="visible")
        states = [
            await locator.nth(index).evaluate(
                """element => {
                  const rect = element.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0 &&
                    rect.top >= 0 && rect.left >= 0 &&
                    rect.bottom <= innerHeight && rect.right <= innerWidth;
                }"""
            )
            for index in range(await locator.count())
        ]
        actual = states[0] if len(states) == 1 else states
        ok = bool(states) and all(states)
    elif action == "assert_text":
        await locator.first.wait_for(state="attached")
        mode = str(step.get("match") or ("nonempty" if expected == "" else "exact"))
        # Presence can precede its asynchronous value (timers, debounced
        # rendering, animation completion). Wait for the asserted text itself
        # within Playwright's bounded action timeout instead of immediately
        # turning a correct delayed update into a Repair sample.
        try:
            await page.wait_for_function(
                "({selector, expected, mode}) => { const readValues = " + _ASSERTION_TEXT_VALUES + ";" + """
              const nodes = [...document.querySelectorAll(selector)];
              const matches = value => mode === 'nonempty'
                ? String(value || '').trim().length > 0
                : mode === 'contains'
                  ? String(value || '').includes(String(expected ?? ''))
                  : String(value || '') === String(expected ?? '');
              const values = readValues(nodes, mode);
              return values.length > 0 && values.every(matches);
                }""",
                arg={
                    "selector": selector,
                    "expected": expected,
                    "mode": mode,
                },
            )
        except Exception:
            # Preserve concrete actual/expected evidence on a normal assertion
            # timeout; outer execution still records the failed assertion.
            pass
        values = await locator.evaluate_all(_ASSERTION_TEXT_VALUES, mode)
        count = len(values)
        actual = values[0] if count == 1 else values
        ok = count > 0 and all(
            _matches(
                value,
                expected,
                mode,
            )
            for value in values
        )
    elif action == "assert_value":
        actual = await locator.input_value()
        if "snapshot" in step:
            snapshot_name = str(step["snapshot"])
            if snapshot_name not in attribute_snapshots:
                raise ActionContractError(
                    f"assert_value references missing snapshot {snapshot_name!r}"
                )
            expected = attribute_snapshots[snapshot_name]
        ok = _matches(actual, expected, str(step.get("match", "exact")))
        if ok and "capture_as" in step:
            attribute_snapshots[str(step["capture_as"])] = actual
    elif action == "assert_count":
        actual = await locator.count()
        expected = int(step["count"])
        ok = actual == expected
    elif action == "assert_number":
        handle = await page.wait_for_function("""spec => {
            const nodes = document.querySelectorAll(spec.selector);
            if (nodes.length !== 1) return false;
            const raw = nodes[0][spec.property];
            if (raw == null || String(raw).trim() === '') return false;
            const value = Number(raw);
            return Number.isFinite(value) && value >= spec.min && value <= spec.max ? {value} : false;
        }""", arg=step, timeout=step.get("timeout_ms", 5000))
        actual = (await handle.json_value())["value"]
        await handle.dispose()
        expected = {"min": step["min"], "max": step["max"]}
        ok = True
    elif action == "assert_computed_style":
        property_name = str(step["property"])
        actual = await locator.evaluate(
            "(element, propertyName) => "
            "getComputedStyle(element).getPropertyValue(propertyName).trim()",
            property_name,
        )
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_property":
        name = str(step["name"])
        actual = await locator.evaluate("(element, key) => element[key]", name)
        ok = actual == expected
    elif action == "assert_scroll":
        actual = await page.evaluate("window.scrollY")
        if "snapshot" in step:
            expected = await page.evaluate(
                "name => (window.__harnessScrollSnapshots || {})[name]",
                str(step["snapshot"]),
            )
            if expected is None:
                raise ActionContractError(
                    f"assert_scroll references missing snapshot {step['snapshot']!r}"
                )
        else:
            expected = int(step["y"])
        ok = abs(int(actual) - expected) <= int(step.get("tolerance", 1))
    elif action == "assert_url":
        actual = urlsplit(page.url).path if str(expected).startswith("/") else page.url
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_hash":
        fragment = urlsplit(page.url).fragment
        actual = f"#{fragment}" if fragment else ""
        ok = _matches(actual, expected, str(step.get("match", "exact")))
    elif action == "assert_attribute":
        actual = await locator.get_attribute(str(step["name"]))
        if "snapshot" in step:
            snapshot_name = str(step["snapshot"])
            if snapshot_name not in attribute_snapshots:
                raise ActionContractError(
                    f"assert_attribute references missing snapshot {snapshot_name!r}"
                )
            expected = attribute_snapshots[snapshot_name]
        mode = str(step.get("match") or ("nonempty" if expected == "" else "exact"))
        if step.get("not", False) and "snapshot" in step:
            name = str(step["name"])
            snapshot_value = expected
            await page.wait_for_function(
                """({selector, name, snapshot}) => {
                  const node = document.querySelector(selector);
                  return node && node.getAttribute(name) !== snapshot;
                }""",
                arg={"selector": resolved_selector, "name": name, "snapshot": snapshot_value},
                timeout=int(step.get("timeout_ms", 5000)),
            )
            actual = await locator.get_attribute(name)
            ok = actual != snapshot_value
        else:
            ok = _matches(actual, expected, mode)
        if step.get("not", False):
            ok = ok if "snapshot" in step else not ok
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
    if resolved_selector != selector:
        output["resolved_selector"] = resolved_selector
    if aria_snapshot is not None:
        output["aria_snapshot"] = aria_snapshot
    return ok, output


async def _resolve_behavioral_selector(page: Any, selector: str, action: str) -> str:
    """Accept stable equivalent selectors produced by an existing component.

    Planner selectors describe the behavior being checked, while a host
    component may expose the value, trend, or status node as the stable DOM
    address.  Only visibility/wait assertions use these bounded aliases; user
    interactions still require the exact control selector.
    """
    if not selector or action not in {"assert_visible", "wait_for"}:
        return selector
    match = re.fullmatch(r'\[data-testid="([^"]+)"\]', selector)
    if not match:
        return selector
    test_id = match.group(1)
    aliases: list[str] = []
    if test_id.startswith("summary-") and not test_id.endswith("-value"):
        aliases.append(f"{test_id}-value")
    for suffix in ("-trend", "-status"):
        if test_id.startswith("metric-") and test_id.endswith(suffix):
            metric = test_id[len("metric-") : -len(suffix)]
            aliases.extend((f"metric-{metric}{suffix}", f"metric-{metric}-reports{suffix}"))
    # Permit a host component to add a stable semantic suffix/prefix while
    # retaining the planner's metric role (value, trend, or status).
    if not aliases:
        aliases = [test_id + suffix for suffix in ("-value", "-trend", "-status")]
    for alias in aliases:
        candidate = f'[data-testid="{alias}"]'
        if await page.locator(candidate).count():
            return candidate
    return selector


async def _execute_webcompass_risk_assertion(
    *, page: Any, step: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Audit one selected WebCompass risk using DOM, AX-like and layout facts.

    The rules are deliberately conservative.  They look only inside the
    planner-addressed target surface (or its action control), and return
    concrete nodes/measurements rather than screenshot similarity.
    """
    selector = str(step["selector"])
    defect_type = str(step["defect_type"])
    # Measure the reached state after finite UI transitions, without changing
    # the page's animation semantics or waiting for decorative infinite loops.
    await page.evaluate("""async () => {
      const animations = document.getAnimations().filter(animation => {
        const end = animation.effect?.getComputedTiming().endTime;
        return animation.playState === 'running' && Number.isFinite(end) && end <= 1000;
      });
      if (!animations.length) return;
      let timer;
      try {
        await Promise.race([
          Promise.all(animations.map(animation => animation.finished.catch(() => {}))),
          new Promise(resolve => { timer = setTimeout(resolve, 1000); })
        ]);
      } finally { clearTimeout(timer); }
    }""")
    output = await page.locator(selector).evaluate_all(
        r"""(roots, [selector, defectType]) => {
          const label = element => {
            if (!element) return '<none>';
            if (element.id) return `#${element.id}`;
            const testId = element.getAttribute('data-testid');
            if (testId) return `[data-testid="${testId}"]`;
            const cls = typeof element.className === 'string'
              ? element.className.trim().split(/\s+/).filter(Boolean)[0] : '';
            return `${element.tagName.toLowerCase()}${cls ? `.${cls}` : ''}`;
          };
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              Number.parseFloat(style.opacity || '1') > 0 && rect.width > 0 && rect.height > 0;
          };
          const activeModals = [...document.querySelectorAll('dialog[open], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]')].filter(visible);
          const inActiveLayer = element => activeModals.every(modal =>
            modal.contains(element) || element.contains(modal));
          const interactive = element => element.matches(
            'button,a[href],input,select,textarea,[role="button"],[role="link"],[tabindex]'
          );
          const all = roots.flatMap(root => [root, ...root.querySelectorAll('*')])
            .filter(visible).slice(0, 240);
          const issues = [];
          const identity = element => {
            const parts = [];
            for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
              if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
              const siblings = node.parentElement ? [...node.parentElement.children].filter(
                item => item.tagName === node.tagName) : [node];
              parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')');
            }
            return parts.join(' > ');
          };
          const add = (kind, element, detail = {}) => {
            if (issues.length < 16) issues.push({kind, element: label(element),
              element_path: identity(element), text: String(element.textContent || '').trim().slice(0, 80), ...detail});
          };
          if (!roots.length) {
            return {passed: true, defect_type: defectType, selector, applicable: false,
              reason: 'target-surface-missing; functional flow records reachability',
              inspected: 0, issues: []};
          }
          const rectOverlap = (a, b) => {
            const width = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
            const height = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
            return {width, height, area: width * height};
          };
          if (defectType === 'Occlusion') {
            const candidates = all.filter(element => inActiveLayer(element) && (interactive(element) ||
              element.hasAttribute('data-testid'))).slice(0, 80);
            for (const element of candidates) {
              const rect = element.getBoundingClientRect();
              if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
              const x = rect.left + rect.width / 2;
              const y = rect.top + rect.height / 2;
              if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight) continue;
              const top = document.elementFromPoint(x, y);
              const clipped = rect.top < 0 || rect.left < 0 || rect.bottom > innerHeight || rect.right > innerWidth;
              let scrollChrome = false;
              if (clipped) for (let node = top; node; node = node.parentElement) {
                if (['fixed', 'sticky'].includes(getComputedStyle(node).position)) {
                  scrollChrome = true;
                  break;
                }
              }
              if (scrollChrome) continue;
              if (top && top !== element && !element.contains(top) && !top.contains(element)) {
                add('foreign-element-covers-center', element, {covering_element: label(top), x, y});
              }
            }
          } else if (defectType === 'Crowding') {
            // Collapsed table boxes intentionally share edges. Their text and
            // child controls remain covered by overlap and spacing checks.
            const tableBoxes = new Set(['TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TH', 'TD']);
            const candidates = all.filter(element => !tableBoxes.has(element.tagName) && inActiveLayer(element) && (interactive(element) ||
              element.hasAttribute('data-testid'))).slice(0, 100);
            for (let i = 0; i < candidates.length; i++) for (let j = i + 1; j < candidates.length; j++) {
              const a = candidates[i], b = candidates[j];
              if (a.contains(b) || b.contains(a)) continue;
              const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
              const horizontalGap = Math.max(rb.left - ra.right, ra.left - rb.right);
              const verticalGap = Math.max(rb.top - ra.bottom, ra.top - rb.bottom);
              const verticalBand = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
              const horizontalBand = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
              if ((verticalBand > 4 && horizontalGap >= 0 && horizontalGap < 2) ||
                  (horizontalBand > 4 && verticalGap >= 0 && verticalGap < 2)) {
                add('less-than-2px-gap', a, {peer: label(b), horizontal_gap: horizontalGap,
                  vertical_gap: verticalGap});
              }
            }
          } else if (defectType === 'Text Overlap') {
            const ownTextRects = element => {
              const rects = [];
              for (const node of element.childNodes) {
                if (node.nodeType !== Node.TEXT_NODE || !String(node.textContent || '').trim()) continue;
                const range = document.createRange();
                range.selectNodeContents(node);
                for (const rect of range.getClientRects()) {
                  if (rect.width > 0 && rect.height > 0) rects.push(rect);
                }
              }
              return rects;
            };
            const candidates = all.filter(element => {
              const own = [...element.childNodes].some(node =>
                node.nodeType === Node.TEXT_NODE && String(node.textContent || '').trim());
              return own && inActiveLayer(element);
            }).slice(0, 120);
            for (let i = 0; i < candidates.length; i++) for (let j = i + 1; j < candidates.length; j++) {
              const a = candidates[i], b = candidates[j];
              if (a.contains(b) || b.contains(a)) continue;
              let strongest = null;
              for (const ra of ownTextRects(a)) for (const rb of ownTextRects(b)) {
                const overlap = rectOverlap(ra, rb);
                if (overlap.area > 16 && overlap.width > 2 && overlap.height > 2 &&
                    (!strongest || overlap.area > strongest.area)) strongest = overlap;
              }
              if (strongest) {
                add('text-rectangles-overlap', a,
                  {peer: label(b), overlap_area: Math.round(strongest.area)});
              }
            }
          } else if (defectType === 'Alignment') {
            for (const container of all.slice(0, 100)) {
              const style = getComputedStyle(container);
              if (!['flex', 'grid', 'inline-flex', 'inline-grid'].includes(style.display)) continue;
              const children = [...container.children].filter(child => visible(child) &&
                !['absolute', 'fixed'].includes(getComputedStyle(child).position));
              if (children.length < 3 || children.length > 20) continue;
              const direction = style.flexDirection || 'row';
              if (style.alignItems.includes('baseline')) continue;
              const coordinate = child => {
                const rect = child.getBoundingClientRect();
                const column = direction.startsWith('column');
                if (style.alignItems === 'center') return column ? (rect.left + rect.right) / 2 : (rect.top + rect.bottom) / 2;
                if (['end', 'flex-end', 'self-end'].includes(style.alignItems)) return column ? rect.right : rect.bottom;
                return column ? rect.left : rect.top;
              };
              let groups = [children];
              if (style.flexWrap !== 'nowrap' || style.display.includes('grid')) {
                groups = [];
                const column = direction.startsWith('column');
                for (const child of children) {
                  const rect = child.getBoundingClientRect();
                  const group = groups.find(items => items.some(peer => {
                    const other = peer.getBoundingClientRect();
                    return column ? Math.min(rect.right, other.right) > Math.max(rect.left, other.left)
                      : Math.min(rect.bottom, other.bottom) > Math.max(rect.top, other.top);
                  }));
                  if (group) group.push(child); else groups.push([child]);
                }
              }
              for (const group of groups) {
                if (group.length < 3) continue;
                const values = group.map(coordinate).sort((a, b) => a - b);
                const median = values[Math.floor(values.length / 2)];
                const outliers = group.filter(child => Math.abs(coordinate(child) - median) > 8);
                if (outliers.length === 1) add('single-repeated-item-misaligned', outliers[0],
                  {container: label(container), tolerance_px: 8});
              }
            }
          } else if (defectType === 'Color Contrast') {
            const rgba = value => {
              const match = String(value || '').match(/[\d.]+/g);
              if (!match || match.length < 3) return null;
              return [Number(match[0]), Number(match[1]), Number(match[2]),
                match.length > 3 ? Number(match[3]) : 1];
            };
            const luminance = color => {
              const c = color.slice(0, 3).map(value => {
                const unit = value / 255;
                return unit <= 0.04045 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
              });
              return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
            };
            const background = element => {
              let node = element;
              while (node) {
                const parsed = rgba(getComputedStyle(node).backgroundColor);
                if (parsed && parsed[3] >= 0.95) return parsed;
                node = node.parentElement;
              }
              return [255, 255, 255, 1];
            };
            for (const element of all) {
              const ownText = [...element.childNodes].some(node =>
                node.nodeType === Node.TEXT_NODE && String(node.textContent || '').trim());
              if (!ownText) continue;
              const style = getComputedStyle(element);
              const fg = rgba(style.color), bg = background(element);
              if (!fg || fg[3] < 0.95) continue;
              const a = luminance(fg), b = luminance(bg);
              const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
              const fontSize = Number.parseFloat(style.fontSize || '16');
              const fontWeight = Number.parseInt(style.fontWeight || '400', 10);
              const large = fontSize >= 24 || (fontSize >= 18.66 && fontWeight >= 700);
              const threshold = large ? 3 : 4.5;
              if (ratio + 0.01 < threshold) add('wcag-text-contrast', element,
                {ratio: Math.round(ratio * 100) / 100, threshold});
            }
          } else if (defectType === 'Overflow') {
            if (document.documentElement.scrollWidth > innerWidth + 2) {
              add('viewport-horizontal-overflow', document.documentElement,
                {scroll_width: document.documentElement.scrollWidth, viewport_width: innerWidth});
            }
            for (const element of all) {
              const style = getComputedStyle(element);
              // Screen-reader-only clipping intentionally keeps accessible text
              // in a one-pixel box. It is not unintended visible overflow.
              const clippedAccessibleText = element.clientWidth <= 1 && element.clientHeight <= 1 &&
                ['absolute', 'fixed'].includes(style.position) &&
                ['hidden', 'clip'].includes(style.overflowX) &&
                (style.clip !== 'auto' || style.clipPath !== 'none');
              if (clippedAccessibleText) continue;
              if (element.scrollWidth > element.clientWidth + 2 &&
                  !['auto', 'scroll'].includes(style.overflowX)) {
                add('unhandled-horizontal-overflow', element,
                  {scroll_width: element.scrollWidth, client_width: element.clientWidth,
                    overflow_x: style.overflowX});
              }
            }
          } else if (defectType === 'Sizing Proportion') {
            for (const element of all.filter(item => item.matches('img,video'))) {
              const naturalWidth = element.naturalWidth || element.videoWidth || 0;
              const naturalHeight = element.naturalHeight || element.videoHeight || 0;
              const rect = element.getBoundingClientRect();
              if (!naturalWidth || !naturalHeight || !rect.width || !rect.height) continue;
              const naturalRatio = naturalWidth / naturalHeight;
              const renderedRatio = rect.width / rect.height;
              const relativeError = Math.abs(renderedRatio - naturalRatio) / naturalRatio;
              if (relativeError > 0.15) add('distorted-media-aspect-ratio', element,
                {natural_ratio: naturalRatio, rendered_ratio: renderedRatio,
                  relative_error: relativeError});
            }
          } else if (defectType === 'Loss of Interactivity') {
            const candidates = all.filter(element => interactive(element) && inActiveLayer(element));
            if (!candidates.length) add('intended-interactive-target-missing', roots[0]);
            for (const element of candidates) {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              const disabled = element.disabled || element.getAttribute('aria-disabled') === 'true';
              const x = Math.max(0, Math.min(innerWidth - 1, rect.left + rect.width / 2));
              const y = Math.max(0, Math.min(innerHeight - 1, rect.top + rect.height / 2));
              const inViewport = rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
              const top = inViewport ? document.elementFromPoint(x, y) : null;
              const blocked = top && top !== element && !element.contains(top) && !top.contains(element);
              if (disabled || style.pointerEvents === 'none' || blocked) add('interaction-blocked', element,
                {disabled: Boolean(disabled), pointer_events: style.pointerEvents,
                  covering_element: blocked ? label(top) : null});
            }
          } else if (defectType === 'Semantic Error') {
            for (const element of all) {
              if (!element.matches('div,span')) continue;
              const hasClick = element.hasAttribute('onclick');
              const keyboardFocusable = element.tabIndex >= 0;
              const role = element.getAttribute('role');
              if ((hasClick || keyboardFocusable) && !role) add('generic-interactive-node-without-role', element,
                {has_onclick: hasClick, tabindex: element.tabIndex});
            }
          } else if (defectType === 'Nesting Error') {
            for (const element of all) {
              if (element.matches('button,a[href],input,select,textarea,[role="button"],[role="link"]')) {
                const nested = element.querySelector(
                  'button,a[href],input,select,textarea,[role="button"],[role="link"]'
                );
                if (nested) add('nested-interactive-controls', element, {nested: label(nested)});
              }
              if (element.matches('main') && element.querySelector('main')) {
                add('nested-main-landmarks', element);
              }
            }
            const ids = new Map();
            for (const element of all.filter(item => item.id)) {
              if (ids.has(element.id)) add('duplicate-id', element, {duplicate_of: label(ids.get(element.id))});
              else ids.set(element.id, element);
            }
          } else if (defectType === 'Missing Attributes') {
            for (const element of all) {
              if (element.matches('img') && !element.hasAttribute('alt')) add('image-missing-alt', element);
              if (element.matches('a') && !element.hasAttribute('href')) add('anchor-missing-href', element);
              if (element.matches('input,select,textarea,button')) {
                const id = element.id;
                const labelled = element.hasAttribute('aria-label') ||
                  element.hasAttribute('aria-labelledby') ||
                  (id && document.querySelector(`label[for="${CSS.escape(id)}"]`)) ||
                  (element.closest('label')) || String(element.textContent || '').trim() ||
                  element.getAttribute('title');
                if (!labelled) add('control-missing-accessible-name', element);
              }
            }
          }
          return {passed: issues.length === 0, defect_type: defectType, selector,
            inspected: all.length, issues};
        }""",
        [selector, defect_type],
    )
    return bool(output.get("passed")), {"actual": output, "expected": {"issues": []}}


def _same_origin_route_url(app_url: str, route: str) -> str:
    """Resolve a planner route without allowing navigation off the app origin."""
    base = urlsplit(app_url)
    target = urlsplit(route)
    segments = target.path.replace("\\", "/").split("/")
    fragment_segments = target.fragment.split("/")
    fragment_ok = not target.fragment or (
        target.fragment.startswith("/")
        and "\\" not in target.fragment
        and "://" not in target.fragment
        and "?" not in target.fragment
        and all(segment not in {".", ".."} for segment in fragment_segments)
    )
    if (
        base.scheme not in {"http", "https"}
        or not base.netloc
        or not route.startswith("/")
        or route.startswith("//")
        or "\\" in route
        or target.scheme
        or target.netloc
        or target.query
        or not fragment_ok
        or any(segment in {".", ".."} for segment in segments)
    ):
        raise ValueError(f"unsafe browser route: {route!r}")
    return urlunsplit((base.scheme, base.netloc, target.path or "/", "", target.fragment))


async def collect_browser_evidence(
    *, app_url: str, checks: list[dict[str, Any]], output_path: Path, headless: bool,
    fail_fast: bool = False, action_timeout_ms: int = 5_000,
    visual_sanity_selectors: list[dict[str, Any]] | None = None,
    capture_screenshots: bool = False,
    screenshot_timeout_ms: int = 15_000,
    screenshot_full_page: bool = False,
    baseline_console_errors: list[str] | None = None,
    lenient_console: bool = False,
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
        payload = {"app_url": app_url, "checks": records, "policy_version": BROWSER_EVIDENCE_POLICY_VERSION}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return payload
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        page = None
        try:
            console_errors: list[str] = []
            console_warnings: list[str] = []
            def record_console_error(message: Any) -> None:
                if message.type == 'warning' or (lenient_console and message.type == 'error'
                        and _is_nonblocking_react_warning(message.text)):
                    console_warnings.append(message.text)
                    return
                if message.type != "error":
                    return
                # Chromium emits a generic duplicate for failed subresources.
                # The response handler below records the exact URL/status, so
                # keeping both loses the resource identity and can also turn
                # its implicit undeclared favicon probe into a false failure.
                if message.text.startswith("Failed to load resource:"):
                    return
                console_errors.append(message.text)

            def record_failed_response(response: Any) -> None:
                if response.status < 400:
                    return
                if urlsplit(response.url).path == "/favicon.ico":
                    return
                console_errors.append(f"HTTP {response.status}: {response.url}")

            async def fresh_page() -> Any:
                created = await browser.new_page(viewport={"width": 1280, "height": 812})
                created.on("console", record_console_error)
                created.on("response", record_failed_response)
                created.on("pageerror", lambda error: console_errors.append(str(getattr(error, "stack", None) or error)))
                # A contract miss is evidence about this one UI check, not a
                # reason to burn the whole evaluation budget on 30s defaults.
                created.set_default_timeout(action_timeout_ms)
                return created

            # Top-level checks are independent experiments. Only adjacent
            # ``__partN`` slices of one harness-split flow share browser state.
            active_route: str | None = None
            active_split_flow: str | None = None
            for check_index, check in enumerate(checks):
                steps = check.get("actions") if isinstance(check, dict) else []
                route = str(check.get("route", "/"))
                check_id = str(check.get("id") or "")
                split_flow = re.sub(r"(?:__part\d+)+$", "", check_id)
                if split_flow == check_id:
                    split_flow = None
                continues_split_flow = bool(
                    page is not None
                    and split_flow
                    and split_flow == active_split_flow
                    and route == active_route
                )
                if not continues_split_flow:
                    if page is not None:
                        await page.close()
                    console_errors.clear()
                    console_warnings.clear()
                    page = await fresh_page()
                    active_route = None
                    attribute_snapshots: dict[str, Any] = {}
                if (continues_split_flow and check.get("origin") == "source_edit_risk_analysis"
                        and records and records[-1].get("status") in {"action_failed", "blocked_by_setup"}
                        and not any(step.get("action") == "assert_webcompass_risk"
                                    for step in records[-1].get("steps") or [])):
                    records.append({"check_id": check_id, "route": route,
                                    "status": "blocked_by_setup", "steps": [],
                                    "blocked_by": records[-1].get("blocked_by") or records[-1].get("check_id")})
                    continue
                risk_continuation = bool(
                    continues_split_flow and check.get("origin") == "source_edit_risk_analysis"
                    and records and any(step.get("action") == "assert_webcompass_risk"
                                        for step in records[-1].get("steps") or [])
                )
                # Risk parts inspect the same reached state. Replaying a toggle,
                # submit, or navigation for each class would change that state.
                if risk_continuation:
                    steps = [step for step in steps if step.get("action") == "assert_webcompass_risk"]
                console_error_start = len(console_errors)
                console_warning_start = len(console_warnings)
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
                        or (
                            current_location.fragment != target_location.fragment
                            and not continues_split_flow
                        )
                    )
                    if not risk_continuation and (route != active_route or location_drifted):
                        await page.goto(
                            route_url,
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        await page.wait_for_timeout(150)
                        active_route = route
                    active_split_flow = split_flow
                    item["url"] = page.url
                except Exception as exc:
                    item.update(
                        {
                            "status": "invalid_test_contract" if isinstance(exc, ValueError) else "navigation_failed",
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
                                capture_scroll_as = step.get("capture_scroll_as")
                                if capture_scroll_as:
                                    await page.locator(str(step["selector"])).first.evaluate(
                                        """(element, name) => {
                                          window.__harnessScrollSnapshots ||= {};
                                          element.addEventListener('click', () => {
                                            window.__harnessScrollSnapshots[name] = window.scrollY;
                                          }, {capture: true, once: true});
                                        }""",
                                        str(capture_scroll_as),
                                    )
                                await page.click(
                                    str(step["selector"]),
                                    button=str(step.get("button", "left")),
                                )
                                result["output"] = (
                                    {
                                        "clicked": True,
                                        "scroll_snapshot": str(capture_scroll_as),
                                        "scroll_y": await page.evaluate(
                                            "name => (window.__harnessScrollSnapshots || {})[name]",
                                            str(capture_scroll_as),
                                        ),
                                    }
                                    if capture_scroll_as
                                    else "clicked"
                                )
                            elif action == "capture_attribute":
                                snapshot_name = str(step["snapshot"])
                                captured_value = await page.locator(
                                    str(step["selector"])
                                ).first.get_attribute(str(step["name"]))
                                if captured_value is None:
                                    raise ActionContractError(
                                        "capture_attribute target attribute is missing"
                                    )
                                attribute_snapshots[snapshot_name] = captured_value
                                result["output"] = {
                                    "snapshot": snapshot_name,
                                    "captured": captured_value,
                                }
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
                                known_keys = {"Tab", "Enter", "Escape", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown"}
                                parts = key.split("+")
                                is_chord = len(parts) > 1 and all(
                                    part in {"Alt", "Control", "ControlOrMeta", "Meta", "Shift"}
                                    for part in parts[:-1]
                                ) and bool(parts[-1])
                                if key not in known_keys and len(key) != 1 and not is_chord and not re.fullmatch(r"F(?:[1-9]|1[0-2])", key):
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
                            elif action == "go_back":
                                await page.go_back(
                                    wait_until="domcontentloaded",
                                    timeout=15_000,
                                )
                                result["output"] = "went back"
                            elif action == "fill":
                                await page.fill(str(step["selector"]), str(step["value"]))
                                result["output"] = "filled"
                            elif action == "select_option":
                                await page.select_option(str(step["selector"]), str(step["value"]))
                                result["output"] = "selected"
                            elif action == "set_hash":
                                await page.evaluate(
                                    "value => { window.location.hash = value; }",
                                    str(step["value"]),
                                )
                                result["output"] = await page.evaluate("window.location.hash")
                            elif action == "set_storage_value":
                                encoding = str(step.get("encoding", "string"))
                                encoded = (
                                    json.dumps(
                                        step.get("value"),
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    if encoding == "json"
                                    else str(step.get("value", ""))
                                )
                                await page.evaluate(
                                    "([kind, key, value]) => "
                                    "(kind === 'local' ? localStorage : sessionStorage)"
                                    ".setItem(key, value)",
                                    [str(step["storage"]), str(step["key"]), encoded],
                                )
                                result["output"] = {
                                    "storage": str(step["storage"]),
                                    "key": str(step["key"]),
                                    "encoded_bytes": len(encoded.encode("utf-8")),
                                }
                            elif action == "set_input_files":
                                payloads = [
                                    {
                                        "name": str(fixture["name"]),
                                        "mimeType": str(fixture["mime_type"]),
                                        "buffer": str(fixture["content"]).encode("utf-8").ljust(
                                            fixture.get("size_bytes", 0), b"\0"),
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
                            elif action == "wait":
                                await page.wait_for_timeout(int(step["milliseconds"]))
                                result["output"] = f"waited {int(step['milliseconds'])}ms"
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
                                    console_errors=[error for error in console_errors[console_error_start:]
                                                    if error not in (baseline_console_errors or [])],
                                    attribute_snapshots=attribute_snapshots,
                                )
                                result["output"] = assertion_output
                                result["ok"] = assertion_ok
                            elif action in HARNESS_ASSERTION_ACTIONS:
                                assertion_ok, assertion_output = (
                                    await _execute_webcompass_risk_assertion(
                                        page=page,
                                        step=step,
                                    )
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
                            selector = str(step.get("selector") or "").strip()
                            if selector and action in {
                                "assert_visible", "assert_in_view", "click", "wait_for"
                            }:
                                try:
                                    result["visibility_diagnostic"] = (
                                        await _visibility_diagnostic(page, selector)
                                    )
                                except Exception:
                                    pass
                            if _is_invalid_test_contract_error(action, exc):
                                result["test_precondition"] = True
                        item["steps"].append(result)
                        # Actions in one check are an ordered user flow. Once a
                        # step fails, later clicks/assertions have no valid
                        # precondition and only multiply timeouts and noisy
                        # Repair evidence. The next top-level check still runs
                        # in its own fresh context.
                        if result.get("ok") is False:
                            break
                finally:
                    pass
                failed_precondition = any(
                    step.get("test_precondition") and not step.get("ok")
                    for step in item["steps"]
                )
                if any(not step.get('ok') for step in item['steps']):
                    item['failure_dom'] = (await page.locator('body').inner_html())[:12000]
                check_console_errors = [error for error in console_errors[console_error_start:]
                                        if error not in (baseline_console_errors or [])]
                if console_warnings[console_warning_start:]:
                    item['console_warnings'] = console_warnings[console_warning_start:]
                if baseline_console_errors:
                    item['baseline_console_errors'] = baseline_console_errors
                if check_console_errors:
                    item["console_errors"] = check_console_errors
                    overlay = page.locator('vite-error-overlay')
                    if await overlay.count():
                        item['build_error'] = (await overlay.inner_text())[:8000]
                contrast_issues = await _severe_contrast_issues(
                    page, visual_sanity_selectors or [], route
                )
                if contrast_issues:
                    item["visual_sanity"] = {
                        "status": "failed",
                        "kind": "severe_text_contrast",
                        "threshold": 1.5,
                        "issues": contrast_issues,
                    }
                item["status"] = (
                    "invalid_test_contract" if failed_precondition
                    else "ok" if (
                        all(step.get("ok") for step in item["steps"])
                        and not check_console_errors
                        and not contrast_issues
                    )
                    else "action_failed"
                )
                next_check = checks[check_index + 1] if check_index + 1 < len(checks) else {}
                next_id = str(next_check.get("id") or "")
                flow_continues = bool(split_flow and
                    re.sub(r"(?:__part\d+)+$", "", next_id) == split_flow and
                    str(next_check.get("route", "/")) == route)
                if capture_screenshots and (item["status"] == "action_failed" or (item["status"] == "ok" and not flow_continues)):
                    screenshot = output_path.with_name(f"{output_path.stem}_state_{check_index + 1}.png")
                    capture_targets = []
                    for fragment in visual_sanity_selectors or []:
                        if fragment.get("route", "/") not in {route, "/" if route == "/index.html" else route}:
                            continue
                        locator = page.locator(str(fragment.get("selector") or "body"))
                        if await locator.count() != 1 or not await locator.is_visible():
                            continue
                        box = await locator.bounding_box()
                        if box and box["width"] > 100 and box["height"] > 100:
                            capture_targets.append((box["width"] * box["height"], locator, fragment["selector"]))
                    if capture_targets:
                        _, target, selector = max(capture_targets, key=lambda entry: entry[0])
                        await target.screenshot(path=str(screenshot), timeout=screenshot_timeout_ms)
                        item["screenshot_selector"] = selector
                    else:
                        await page.screenshot(path=str(screenshot), timeout=screenshot_timeout_ms, full_page=screenshot_full_page)
                    item["screenshot"] = screenshot.name
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
    payload = {"app_url": app_url, "checks": records, "policy_version": BROWSER_EVIDENCE_POLICY_VERSION}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload
