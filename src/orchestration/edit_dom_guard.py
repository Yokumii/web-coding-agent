"""Semantic regression guard for forward edit tasks.

This deliberately compares browser semantics rather than screenshots.  A seed
baseline is reduced to independently identifiable top-level surfaces and the
meaningful DOM/ARIA tree inside each surface.  An edit may name a small set of
surfaces it intends to change; every other baseline surface must survive
unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from src.config import HarnessConfig
from src.orchestration.browser_evidence import _same_origin_route_url
from src.orchestration.file_comm import FileComm
from src.utils.playwright_browser import launch_chromium


BASELINE_NAME = "edit_dom_baseline.json"


def sprint_baseline_name(sprint_num: int) -> str:
    return f"edit_dom_source_sprint_{sprint_num}.json"


def repair_baseline_name(round_num: int) -> str:
    return f"repair_dom_source_round_{round_num}.json"


def is_forward_edit(workdir: Path) -> bool:
    return (workdir / "seed_manifest.json").is_file()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _align_single_route_baseline(
    baseline: dict[str, Any],
    current: dict[str, Any],
    scope: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefix a legacy single-route frame before routed union comparison.

    A Sprint baseline captured before a new page exists has local keys such as
    ``main``. Evaluating the later multi-page union necessarily emits routed
    keys such as ``/::main``. They describe the same protected page and must be
    aligned before comparison; otherwise adding a route falsely looks like the
    existing page was removed and recreated.
    """
    if baseline.get("routes") is not None or not isinstance(
        current.get("routes"), list
    ):
        return baseline, scope
    items = [
        item
        for item in baseline.get("fragments", [])
        if isinstance(item, dict) and item.get("key")
    ]
    default_route = str(items[0].get("route", "/")) if items else "/"
    prefix = f"{default_route}::"
    aligned = copy.deepcopy(baseline)
    aligned_scope = copy.deepcopy(scope)

    def routed(key: str) -> str:
        return key if key.startswith(prefix) else prefix + key

    for collection in ("roots", "fragments"):
        for item in aligned.get(collection, []):
            if not isinstance(item, dict) or not item.get("key"):
                continue
            local_key = str(item["key"])
            item["key"] = routed(local_key)
            item["local_key"] = local_key
            item["route"] = str(item.get("route", default_route))
            if item.get("parent_key"):
                item["parent_key"] = routed(str(item["parent_key"]))
    aligned["routes"] = [default_route]
    for field in ("allowed_root_keys", "allowed_fragment_keys"):
        values = aligned_scope.get(field)
        if isinstance(values, list):
            aligned_scope[field] = [
                routed(value) if isinstance(value, str) else value for value in values
            ]
    return aligned, aligned_scope


def compare_contract(
    baseline: dict[str, Any], current: dict[str, Any], scope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a compact, deterministic diff independent of layout or pixels."""
    scope = scope or {}
    if baseline.get("version") == 4:
        baseline, scope = _align_single_route_baseline(baseline, current, scope)
    allowed = scope.get("allowed_root_keys", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return {"passed": False, "reason": "invalid edit scope: allowed_root_keys must be a string list"}
    if baseline.get("version") == 4:
        if baseline.get("stable") is not True or current.get("stable") is not True:
            return {
                "passed": False,
                "reason": "semantic baseline/current snapshot is unstable",
                "baseline_unstable_fragments": baseline.get("unstable_fragment_keys", []),
                "current_unstable_fragments": current.get("unstable_fragment_keys", []),
            }
        return _compare_fragment_contract(baseline, current, scope)

    multi_route = baseline.get("version") == 3
    if len(set(allowed)) != len(allowed):
        return {"passed": False, "reason": "invalid edit scope: allowed roots must be distinct"}
    roots_by_key = {
        str(item.get("key")): item
        for item in baseline.get("roots", [])
        if isinstance(item, dict) and item.get("key")
    }
    if multi_route:
        target_routes = set(scope.get("target_routes") or [])
        protected_routes = set(scope.get("protected_routes") or [])
        allowed_route_counts: dict[str, int] = {}
        for key in allowed:
            route = str((roots_by_key.get(key) or {}).get("route", ""))
            if not route or route not in target_routes or route in protected_routes:
                return {
                    "passed": False,
                    "reason": "invalid edit scope: allowed root is outside target routes",
                    "root": key,
                }
            allowed_route_counts[route] = allowed_route_counts.get(route, 0) + 1
        if any(count > 2 for count in allowed_route_counts.values()):
            return {
                "passed": False,
                "reason": "invalid edit scope: at most two roots per target route may be changed",
            }
    elif len(allowed) > 2:
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
    if added:
        target_routes = set(scope.get("target_routes") or [])
        for key in added:
            route = str(
                next(
                    (
                        item.get("route", "")
                        for item in current.get("roots", [])
                        if isinstance(item, dict) and item.get("key") == key
                    ),
                    "",
                )
            )
            if not allow_new or (multi_route and route not in target_routes):
                violations.append({"root": key, "kind": "unexpected_added"})
    return {
        "passed": not violations,
        "mode": "semantic_dom_contract",
        "allowed_root_keys": allowed,
        "allow_new_roots": allow_new,
        "violations": violations,
        "baseline_root_count": len(before),
        "current_root_count": len(after),
    }


def _compare_fragment_contract(
    baseline: dict[str, Any], current: dict[str, Any], scope: dict[str, Any]
) -> dict[str, Any]:
    """Protect the semantic complement of the smallest declared DOM fragments."""
    allowed = scope.get("allowed_fragment_keys", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return {
            "passed": False,
            "reason": "invalid edit scope: allowed_fragment_keys must be a string list",
        }
    if len(set(allowed)) != len(allowed):
        return {
            "passed": False,
            "reason": "invalid edit scope: allowed fragments must be distinct",
        }
    expected_new = scope.get("expected_new_fragments", [])
    if not isinstance(expected_new, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("selector"), str)
        or not item.get("selector")
        or not isinstance(item.get("route"), str)
        or not isinstance(item.get("max_count"), int)
        or isinstance(item.get("max_count"), bool)
        or not 1 <= item.get("max_count") <= 50
        for item in expected_new
    ):
        return {
            "passed": False,
            "reason": (
                "invalid edit scope: expected_new_fragments must contain "
                "route/selector and max_count in 1..50"
            ),
        }

    before_items = {
        str(item["key"]): item
        for item in baseline.get("fragments", [])
        if isinstance(item, dict) and item.get("key")
    }
    after_items = {
        str(item["key"]): item
        for item in current.get("fragments", [])
        if isinstance(item, dict) and item.get("key")
    }
    unknown = sorted(set(allowed) - set(before_items))
    if unknown:
        return {
            "passed": False,
            "reason": "invalid edit scope: unknown baseline fragments",
            "unknown_fragments": unknown,
        }
    target_routes = set(scope.get("target_routes") or [])
    protected_routes = set(scope.get("protected_routes") or [])
    expected_new_routes = scope.get("expected_new_routes", [])
    if not isinstance(expected_new_routes, list) or not all(
        isinstance(route, str) and route for route in expected_new_routes
    ):
        return {
            "passed": False,
            "reason": "invalid edit scope: expected_new_routes must be a string list",
        }
    expected_new_route_set = set(expected_new_routes)
    baseline_routes = {
        str(item.get("route", "/")) for item in before_items.values()
    }
    if (
        len(expected_new_route_set) != len(expected_new_routes)
        or expected_new_route_set & protected_routes
        or (target_routes and not expected_new_route_set <= target_routes)
        or expected_new_route_set & baseline_routes
    ):
        return {
            "passed": False,
            "reason": "invalid edit scope: expected new routes must be absent target routes",
        }
    route_counts: dict[str, int] = {}
    for key in allowed:
        route = str(before_items[key].get("route", "/"))
        if target_routes and (route not in target_routes or route in protected_routes):
            return {
                "passed": False,
                "reason": "invalid edit scope: allowed fragment is outside target routes",
                "fragment": key,
            }
        route_counts[route] = route_counts.get(route, 0) + 1
    for contract in expected_new:
        route = str(contract["route"])
        if target_routes and (route not in target_routes or route in protected_routes):
            return {
                "passed": False,
                "reason": "invalid edit scope: expected fragment is outside target routes",
                "selector": contract["selector"],
            }
        route_counts[route] = route_counts.get(route, 0) + 1
    if any(count > 4 for count in route_counts.values()):
        return {
            "passed": False,
            "reason": "invalid edit scope: at most four fragments per target route may be changed",
        }

    def parent_chain(key: str, items: dict[str, dict[str, Any]]) -> list[str]:
        output: list[str] = []
        parent = (items.get(key) or {}).get("parent_key")
        visited = {key}
        while isinstance(parent, str) and parent and parent not in visited:
            visited.add(parent)
            output.append(parent)
            parent = (items.get(parent) or {}).get("parent_key")
        return output

    def depth(key: str) -> int:
        return len(parent_chain(key, after_items))

    def selector_matches(selector: str, anchors: set[str]) -> bool:
        normalized = selector.replace("='", '="').replace("']", '"]')
        return selector in anchors or normalized in anchors

    # First identify roots of expected new semantic subtrees. Their descendant
    # fragments are represented by the matched root fingerprint, but unrelated
    # new siblings still need an independent contract.
    expected_hits = [0] * len(expected_new)
    expected_roots: set[str] = set()
    violations: list[dict[str, str]] = []
    added_keys = sorted(
        set(after_items) - set(before_items), key=lambda key: (depth(key), key)
    )
    for key in added_keys:
        if any(parent in allowed for parent in parent_chain(key, after_items)):
            continue
        if any(parent in expected_roots for parent in parent_chain(key, after_items)):
            continue
        item = after_items[key]
        anchors = {str(anchor) for anchor in item.get("anchors", [])}
        route = str(item.get("route", "/"))
        if route in expected_new_route_set:
            continue
        matched_index = next(
            (
                index
                for index, contract in enumerate(expected_new)
                if expected_hits[index] < int(contract["max_count"])
                and contract["route"] == route
                and selector_matches(str(contract["selector"]), anchors)
            ),
            None,
        )
        if matched_index is None:
            violations.append({"fragment": key, "kind": "unexpected_added"})
        else:
            expected_hits[matched_index] += 1
            expected_roots.add(key)
    selector_counts = {
        (str(item.get("route")), str(item.get("selector"))): int(item.get("count"))
        for item in current.get("selector_counts", [])
        if isinstance(item, dict)
        and isinstance(item.get("route"), str)
        and isinstance(item.get("selector"), str)
        and isinstance(item.get("count"), int)
        and not isinstance(item.get("count"), bool)
    }
    observed_counts = [
        selector_counts.get(
            (str(contract["route"]), str(contract["selector"])), expected_hits[index]
        )
        for index, contract in enumerate(expected_new)
    ]
    for index, hits in enumerate(observed_counts):
        if hits != int(expected_new[index]["max_count"]):
            contract = expected_new[index]
            violations.append(
                {
                    "fragment": f"{contract['route']}::{contract['selector']}",
                    "kind": "expected_addition_missing",
                }
            )

    # Ancestor fingerprints necessarily change when an allowed or expected
    # descendant changes. Independently keyed siblings remain protected.
    # An allowed fragment is a semantic subtree boundary. Changes to its own
    # descendants are target-local, while independently keyed siblings under
    # the same parent remain protected. Without this closure a dynamic list
    # container could be authorized but every item/button insertion inside it
    # would still be rejected as collateral change.
    exempt = {
        key
        for key in before_items
        if key in allowed
        or any(parent in allowed for parent in parent_chain(key, before_items))
    }
    for key in allowed:
        exempt.update(parent_chain(key, before_items))
    for key in expected_roots:
        exempt.update(
            parent for parent in parent_chain(key, after_items) if parent in before_items
        )
    for key, item in before_items.items():
        if key in exempt:
            continue
        if key not in after_items:
            violations.append({"fragment": key, "kind": "removed"})
        elif item.get("fingerprint") != after_items[key].get("fingerprint"):
            violations.append({"fragment": key, "kind": "semantic_changed"})
    return {
        "passed": not violations,
        "mode": "semantic_fragment_contract",
        "allowed_fragment_keys": allowed,
        "expected_new_fragments": expected_new,
        "expected_new_routes": expected_new_routes,
        "expected_new_hits": observed_counts,
        "violations": violations,
        "baseline_fragment_count": len(before_items),
        "current_fragment_count": len(after_items),
    }


async def _snapshot_semantic_dom_v3(
    app_url: str, *, headless: bool, routes: list[str] | None = None
) -> dict[str, Any]:
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            snapshot_script = """
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
                const quoted = value => JSON.stringify(String(value || ''));
                const anchorSet = new Set();
                for (const target of [el, ...el.querySelectorAll('[id],[class],[data-testid],[role],[aria-label],a[href],button,input,select,textarea,summary')]) {
                  if (target.id) anchorSet.add(`#${target.id}`);
                  for (const className of [...target.classList].slice(0, 4)) anchorSet.add(`.${className}`);
                  for (const name of ['data-testid', 'role', 'aria-label', 'name']) {
                    if (target.hasAttribute(name)) anchorSet.add(`[${name}=${quoted(target.getAttribute(name))}]`);
                  }
                  if (/^(a|button|input|select|textarea|summary)$/.test(target.tagName.toLowerCase())) {
                    anchorSet.add(target.tagName.toLowerCase());
                  }
                  if (target.tagName.toLowerCase() === 'a' && target.hasAttribute('href')) {
                    anchorSet.add(`a[href=${quoted(target.getAttribute('href'))}]`);
                  }
                }
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
                return {
                  key: ordinal ? `${raw}#${ordinal + 1}` : raw,
                  anchors: [...anchorSet].sort().slice(0, 120),
                  tree: {semantic: node(el), focusables}
                };
              });
            }
            """
            requested_routes = list(dict.fromkeys(routes or ["/"]))
            collected: list[dict[str, Any]] = []
            for route in requested_routes:
                route_url = (
                    _same_origin_route_url(app_url, route)
                    if routes is not None
                    else app_url
                )
                await page.goto(route_url, wait_until="networkidle", timeout=30_000)
                roots = await page.evaluate(snapshot_script)
                for item in roots:
                    local_key = str(item["key"])
                    root = {
                        "key": (
                            f"{route}::{local_key}"
                            if routes is not None
                            else local_key
                        ),
                        "fingerprint": _fingerprint(item["tree"]),
                        "anchors": item.get("anchors", []),
                    }
                    if routes is not None:
                        root.update({"local_key": local_key, "route": route})
                    collected.append(root)
            return {
                "version": 3 if routes is not None else 2,
                "url": app_url,
                **({"routes": requested_routes} if routes is not None else {}),
                "roots": collected,
            }
        finally:
            await browser.close()


async def snapshot_semantic_dom(
    app_url: str,
    *,
    headless: bool,
    routes: list[str] | None = None,
    selector_contracts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture a stable, route-aware semantic frame with nested fragments."""
    snapshot_script = """
    () => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const quoted = value => JSON.stringify(String(value || ''));
      const relevant = el => {
        const tag = el.tagName.toLowerCase();
        return /^(a|button|input|select|textarea|summary|dialog|main|nav|header|footer|aside|section|article|form|h1|h2|h3|h4|h5|h6)$/.test(tag)
          || el.hasAttribute('role') || [...el.attributes].some(a => a.name.startsWith('aria-'));
      };
      const semanticChildren = el => [...el.children].flatMap(child =>
        relevant(child) ? [semanticNode(child)] : semanticChildren(child)
      );
      const semanticNode = el => {
        const attrs = {};
        for (const name of ['role','aria-label','aria-labelledby','aria-describedby','aria-expanded','aria-selected','aria-checked','aria-current','aria-disabled','aria-hidden','aria-pressed','hidden','disabled','type','name','href','tabindex']) {
          if (el.hasAttribute(name)) attrs[name] = el.getAttribute(name) ?? '';
        }
        return {tag: el.tagName.toLowerCase(), attrs, text: clean(el.textContent).slice(0, 300), children: semanticChildren(el)};
      };
      const anchors = el => {
        const anchorSet = new Set();
        for (const target of [el, ...el.querySelectorAll('[id],[class],[data-testid],[role],[aria-label],a[href],button,input,select,textarea,summary')]) {
          if (target.id) anchorSet.add(`#${target.id}`);
          for (const className of [...target.classList].slice(0, 4)) anchorSet.add(`.${className}`);
          for (const name of ['data-testid', 'role', 'aria-label', 'name']) {
            if (target.hasAttribute(name)) anchorSet.add(`[${name}=${quoted(target.getAttribute(name))}]`);
          }
          if (/^(a|button|input|select|textarea|summary)$/.test(target.tagName.toLowerCase())) anchorSet.add(target.tagName.toLowerCase());
          if (target.tagName.toLowerCase() === 'a' && target.hasAttribute('href')) anchorSet.add(`a[href=${quoted(target.getAttribute('href'))}]`);
        }
        return [...anchorSet].sort().slice(0, 120);
      };
      const rawKey = el => {
        const label = clean(el.getAttribute('aria-label'));
        return el.getAttribute('data-testid') || el.id
          || (el.getAttribute('role') ? `${el.getAttribute('role')}:${label || 'unnamed'}` : '')
          || `${el.tagName.toLowerCase()}:${label || 'unnamed'}`;
      };
      const rootSelector = ['body > *','[data-testid]','[role]','header','nav','main','footer','aside','section','article','form','dialog'].join(',');
      const rawRoots = [...document.querySelectorAll(rootSelector)].filter(el => {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (el.parentElement === document.body && !relevant(el)
            && el.querySelector('header,nav,main,footer,aside,section,article,form,dialog,[data-testid],[role]')) return false;
        return true;
      });
      const roots = rawRoots.filter(el => !rawRoots.some(parent => parent !== el && parent.contains(el)));
      const fragmentSelector = ['[data-testid]','[id]','[role]','header','nav','main','footer','aside','section','article','form','dialog','a[href]','button','input','select','textarea','summary'].join(',');
      const fragments = [...document.querySelectorAll(fragmentSelector)].slice(0, 501);
      const overflow = fragments.length > 500;
      const boundedFragments = fragments.slice(0, 500);
      const assignKeys = candidates => {
        const seen = new Map();
        const keyByElement = new Map();
        for (const el of candidates) {
          const raw = rawKey(el);
          const ordinal = seen.get(raw) || 0;
          seen.set(raw, ordinal + 1);
          keyByElement.set(el, ordinal ? `${raw}#${ordinal + 1}` : raw);
        }
        return keyByElement;
      };
      const rootKeys = assignKeys(roots);
      const fragmentKeys = assignKeys(boundedFragments);
      const focusables = el => [el, ...el.querySelectorAll('a[href],button,input,select,textarea,summary,[tabindex]')]
        .filter((control, index, items) => items.indexOf(control) === index)
        .filter(control => {
          const style = getComputedStyle(control);
          return !control.hasAttribute('disabled') && style.display !== 'none' && style.visibility !== 'hidden' && control.getAttribute('tabindex') !== '-1';
        })
        .map(control => ({
          tag: control.tagName.toLowerCase(), role: control.getAttribute('role'),
          name: clean(control.getAttribute('aria-label') || control.textContent || control.value),
          href: control.getAttribute('href'), type: control.getAttribute('type'), tabIndex: control.tabIndex,
        }));
      const records = (candidates, keys, includeParents) => candidates.map(el => {
        let parent = el.parentElement;
        while (includeParents && parent && !fragmentKeys.has(parent)) parent = parent.parentElement;
        return {
          key: keys.get(el), parent_key: includeParents && parent ? fragmentKeys.get(parent) : null,
          anchors: anchors(el), tree: {semantic: semanticNode(el), focusables: focusables(el)},
        };
      });
      return {overflow, roots: records(roots, rootKeys, false), fragments: records(boundedFragments, fragmentKeys, true)};
    }
    """

    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=headless)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            requested_routes = list(dict.fromkeys(routes or ["/"]))
            collected_roots: list[dict[str, Any]] = []
            collected_fragments: list[dict[str, Any]] = []
            selector_counts: list[dict[str, Any]] = []
            unstable_fragment_keys: list[str] = []
            for route in requested_routes:
                route_url = _same_origin_route_url(app_url, route) if routes is not None else app_url
                await page.goto(route_url, wait_until="networkidle", timeout=30_000)
                first = await page.evaluate(snapshot_script)
                await page.wait_for_timeout(150)
                second = await page.evaluate(snapshot_script)
                for contract in selector_contracts or []:
                    if not isinstance(contract, dict) or contract.get("route") != route:
                        continue
                    selector = contract.get("selector")
                    if not isinstance(selector, str) or not selector:
                        raise RuntimeError("semantic selector-count contract is malformed")
                    selector_counts.append(
                        {
                            "route": route,
                            "selector": selector,
                            "count": await page.locator(selector).count(),
                        }
                    )
                if first.get("overflow") or second.get("overflow"):
                    raise RuntimeError(
                        f"semantic fragment limit exceeded on route {route}; refusing an incomplete baseline"
                    )
                prefix = f"{route}::" if routes is not None else ""

                def normalize(item: dict[str, Any]) -> dict[str, Any]:
                    local_key = str(item["key"])
                    normalized: dict[str, Any] = {
                        "key": prefix + local_key,
                        "fingerprint": _fingerprint(item["tree"]),
                        "anchors": item.get("anchors", []),
                        "route": route,
                    }
                    parent_key = item.get("parent_key")
                    if parent_key:
                        normalized["parent_key"] = prefix + str(parent_key)
                    if routes is not None:
                        normalized["local_key"] = local_key
                    return normalized

                first_roots = [normalize(item) for item in first["roots"]]
                first_fragments = [normalize(item) for item in first["fragments"]]
                second_fragments = [normalize(item) for item in second["fragments"]]
                first_map = {item["key"]: item["fingerprint"] for item in first_fragments}
                second_map = {item["key"]: item["fingerprint"] for item in second_fragments}
                unstable_fragment_keys.extend(
                    key for key in set(first_map) | set(second_map)
                    if first_map.get(key) != second_map.get(key)
                )
                collected_roots.extend(first_roots)
                collected_fragments.extend(first_fragments)
            return {
                "version": 4,
                "url": app_url,
                "stable": not unstable_fragment_keys,
                "unstable_fragment_keys": sorted(set(unstable_fragment_keys)),
                **({"routes": requested_routes} if routes is not None else {}),
                **({"selector_counts": selector_counts} if selector_contracts else {}),
                "roots": collected_roots,
                "fragments": collected_fragments,
            }
        finally:
            await browser.close()


async def capture_baseline(
    *, workdir: Path, file_comm: FileComm, config: HarnessConfig, app_url: str,
    routes: list[str] | None = None,
) -> dict[str, Any]:
    snapshot = await snapshot_semantic_dom(
        app_url, headless=config.playwright_headless, routes=routes
    )
    if snapshot.get("stable") is not True:
        raise RuntimeError(
            "semantic baseline is unstable across two samples: "
            + ", ".join(snapshot.get("unstable_fragment_keys", []))
        )
    path = file_comm.dir / BASELINE_NAME
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


async def capture_sprint_source_baseline(
    *, file_comm: FileComm, config: HarnessConfig, app_url: str, sprint_num: int,
    routes: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze the accepted source state for one edit sprint.

    Comparing every later sprint to the original seed incorrectly labels an
    earlier accepted sprint as collateral damage.  Each sprint therefore gets
    its own immutable frame; repair rounds reuse the same frame.
    """
    path = file_comm.dir / sprint_baseline_name(sprint_num)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    snapshot = await snapshot_semantic_dom(
        app_url, headless=config.playwright_headless, routes=routes
    )
    if snapshot.get("stable") is not True:
        raise RuntimeError(
            "semantic sprint baseline is unstable across two samples: "
            + ", ".join(snapshot.get("unstable_fragment_keys", []))
        )
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


async def evaluate_guard(*, workdir: Path, file_comm: FileComm, config: HarnessConfig, app_url: str, round_num: int, sprint_num: int | None = None) -> dict[str, Any] | None:
    repair_path = file_comm.dir / repair_baseline_name(round_num)
    sprint_path = (
        file_comm.dir / sprint_baseline_name(sprint_num)
        if sprint_num is not None else None
    )
    path = (
        repair_path if repair_path.is_file()
        else sprint_path if sprint_path is not None and sprint_path.is_file()
        else file_comm.dir / BASELINE_NAME
    )
    if not path.is_file():
        return None
    baseline = json.loads(path.read_text(encoding="utf-8"))
    scope_path = file_comm.dir / f"edit_scope_round_{round_num}.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8")) if scope_path.is_file() else None
    baseline_routes = baseline.get("routes")
    target_routes = (scope or {}).get("target_routes") or []
    current_routes = sorted(
        {
            str(route)
            for route in (
                list(baseline_routes)
                if isinstance(baseline_routes, list)
                else [
                    item.get("route", "/")
                    for item in baseline.get("fragments", [])
                    if isinstance(item, dict)
                ]
            )
            if route
        }
        | {str(route) for route in target_routes if route}
    )
    current = await snapshot_semantic_dom(
        app_url,
        headless=config.playwright_headless,
        routes=current_routes or None,
        selector_contracts=(scope or {}).get("expected_new_fragments") or None,
    )
    result = compare_contract(baseline, current, scope)
    result["baseline_file"] = f".harness/{path.name}"
    result["scope_file"] = f".harness/{scope_path.name}" if scope_path.is_file() else None
    return result
