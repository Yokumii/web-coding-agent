"""Semantic regression guard for forward edit tasks.

This deliberately compares browser semantics rather than screenshots.  A seed
baseline is reduced to independently identifiable top-level surfaces and the
meaningful DOM/ARIA tree inside each surface.  An edit may name a small set of
surfaces it intends to change; every other baseline surface must survive
unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.utils.playwright_browser import launch_chromium


BASELINE_NAME = "edit_dom_baseline.json"


def is_forward_edit(workdir: Path) -> bool:
    return (workdir / "seed_manifest.json").is_file()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def compare_contract(
    baseline: dict[str, Any], current: dict[str, Any], scope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a compact, deterministic diff independent of layout or pixels."""
    scope = scope or {}
    allowed = scope.get("allowed_root_keys", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return {"passed": False, "reason": "invalid edit scope: allowed_root_keys must be a string list"}
    if len(allowed) > 2 or len(set(allowed)) != len(allowed):
        return {"passed": False, "reason": "invalid edit scope: at most two distinct roots may be changed"}

    before = {item["key"]: item["fingerprint"] for item in baseline.get("roots", [])}
    after = {item["key"]: item["fingerprint"] for item in current.get("roots", [])}
    unknown = sorted(set(allowed) - set(before))
    if unknown:
        return {"passed": False, "reason": "invalid edit scope: unknown baseline roots", "unknown_roots": unknown}

    removed = sorted(key for key in before if key not in after and key not in allowed)
    changed = sorted(
        key for key in before
        if key in after and before[key] != after[key] and key not in allowed
    )
    added = sorted(key for key in after if key not in before)
    allow_new = scope.get("allow_new_roots") is True
    violations: list[dict[str, str]] = []
    violations += [{"root": key, "kind": "removed"} for key in removed]
    violations += [{"root": key, "kind": "semantic_changed"} for key in changed]
    if added and not allow_new:
        violations += [{"root": key, "kind": "unexpected_added"} for key in added]
    return {
        "passed": not violations,
        "mode": "semantic_dom_contract",
        "allowed_root_keys": allowed,
        "allow_new_roots": allow_new,
        "violations": violations,
        "baseline_root_count": len(before),
        "current_root_count": len(after),
    }


async def _snapshot(app_url: str, *, headless: bool) -> dict[str, Any]:
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(app_url, wait_until="networkidle", timeout=30_000)
            roots = await page.evaluate("""
            () => {
              const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const relevant = el => {
                const tag = el.tagName.toLowerCase();
                return /^(a|button|input|select|textarea|summary|dialog|main|nav|header|footer|aside|section|article|h1|h2|h3|h4|h5|h6)$/.test(tag)
                  || el.hasAttribute('role') || [...el.attributes].some(a => a.name.startsWith('aria-'));
              };
              const node = el => {
                const attrs = {};
                for (const name of ['role','aria-label','aria-labelledby','aria-describedby','aria-expanded','aria-selected','aria-checked','aria-current','aria-disabled','type','name','href','tabindex']) {
                  if (el.hasAttribute(name)) attrs[name] = el.getAttribute(name);
                }
                const children = [...el.children].flatMap(child => relevant(child) ? [node(child)] : [...child.querySelectorAll('*')].filter(relevant).map(node));
                return {tag: el.tagName.toLowerCase(), attrs, text: clean(el.innerText).slice(0, 300), children};
              };
              const candidateSelector = [
                'body > *', '[data-testid]', '[role]', 'header', 'nav', 'main',
                'footer', 'aside', 'section', 'article', 'form'
              ].join(',');
              const rawCandidates = [...document.querySelectorAll(candidateSelector)].filter(el => {
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                // A framework mount point such as #root is not independently
                // useful when it merely encloses real semantic surfaces.
                if (el.parentElement === document.body && !relevant(el)
                    && el.querySelector('header,nav,main,footer,aside,section,article,form,[data-testid],[role]')) return false;
                return true;
              });
              // Contracts must be non-overlapping.  Otherwise a filter edit to
              // <main> would also falsely look like twelve unrelated article
              // edits inside it.  Keep the outermost meaningful surface only.
              const candidates = rawCandidates.filter(el => !rawCandidates.some(
                parent => parent !== el && parent.contains(el)
              ));
              const seen = new Map();
              return candidates.map(el => {
                const label = clean(el.getAttribute('aria-label'));
                const raw = el.getAttribute('data-testid') || el.id
                  || (el.getAttribute('role') ? `${el.getAttribute('role')}:${label || 'unnamed'}` : '')
                  || `${el.tagName.toLowerCase()}:${label || 'unnamed'}`;
                const ordinal = seen.get(raw) || 0;
                seen.set(raw, ordinal + 1);
                const focusables = [el, ...el.querySelectorAll('a[href],button,input,select,textarea,summary,[tabindex]')]
                  .filter((control, index, items) => items.indexOf(control) === index)
                  .filter(control => {
                    const style = getComputedStyle(control);
                    return !control.hasAttribute('disabled') && style.display !== 'none' && style.visibility !== 'hidden'
                      && control.getAttribute('tabindex') !== '-1';
                  })
                  .map(control => {
                    const prior = document.activeElement;
                    control.focus({preventScroll: true});
                    const receivesFocus = document.activeElement === control;
                    if (prior instanceof HTMLElement) prior.focus({preventScroll: true});
                    return {
                      tag: control.tagName.toLowerCase(),
                      role: control.getAttribute('role'),
                      name: clean(control.getAttribute('aria-label') || control.innerText || control.value),
                      href: control.getAttribute('href'),
                      type: control.getAttribute('type'),
                      tabIndex: control.tabIndex,
                      receivesFocus,
                    };
                  });
                return {key: ordinal ? `${raw}#${ordinal + 1}` : raw, tree: {semantic: node(el), focusables}};
              });
            }
            """)
            return {
                "version": 1,
                "url": app_url,
                "roots": [{"key": item["key"], "fingerprint": _fingerprint(item["tree"])} for item in roots],
            }
        finally:
            await browser.close()


async def capture_baseline(*, workdir: Path, file_comm: FileComm, config: HarnessConfig, app_url: str) -> dict[str, Any]:
    snapshot = await _snapshot(app_url, headless=config.playwright_headless)
    path = file_comm.dir / BASELINE_NAME
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


async def evaluate_guard(*, workdir: Path, file_comm: FileComm, config: HarnessConfig, app_url: str, round_num: int) -> dict[str, Any] | None:
    path = file_comm.dir / BASELINE_NAME
    if not is_forward_edit(workdir) or not path.is_file():
        return None
    baseline = json.loads(path.read_text(encoding="utf-8"))
    scope_path = file_comm.dir / f"edit_scope_round_{round_num}.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8")) if scope_path.is_file() else None
    current = await _snapshot(app_url, headless=config.playwright_headless)
    result = compare_contract(baseline, current, scope)
    result["scope_file"] = f".harness/{scope_path.name}" if scope_path.is_file() else None
    return result
