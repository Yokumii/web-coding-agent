"""Harness-owned guidance for the shortest defensible edit path.

The counterfactual minimality certificate answers a post-hoc question: could a
finished patch be made smaller?  This module answers the earlier operational
question: where may an editor mutate in the first place?

For scoped edits/repairs the harness derives a change cone from executable UI
checks, the frozen semantic DOM, source anchors, and explicit dependency edges.
The resulting policy is consumed by mutation tools, so it is an execution
boundary rather than a prompt preference.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


PLAN_VERSION = "minimal-path-plan-v3"
SUPPORTED_PLAN_VERSIONS = {"minimal-path-plan-v1", "minimal-path-plan-v2", PLAN_VERSION}
CODE_EXTENSIONS = {
    ".css",
    ".ets",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".json5",
    ".jsx",
    ".qml",
    ".scss",
    ".svelte",
    ".svg",
    ".ts",
    ".tsx",
    ".vue",
    ".wxml",
    ".wxss",
}
BEHAVIOR_EXTENSIONS = {".ets", ".js", ".jsx", ".qml", ".svelte", ".ts", ".tsx", ".vue"}
MARKUP_EXTENSIONS = {
    ".ets",
    ".htm",
    ".html",
    ".jsx",
    ".qml",
    ".svelte",
    ".tsx",
    ".vue",
    ".wxml",
}
STYLE_EXTENSIONS = {".css", ".scss", ".svelte", ".vue", ".wxss"}
INTERACTION_ACTIONS = {
    "check",
    "click",
    "dblclick",
    "drag",
    "drag_and_drop",
    "emulate_media",
    "fill",
    "focus",
    "hover",
    "key_press",
    "press",
    "scroll",
    "select",
    "select_option",
    "set_input_files",
    "tap",
    "type",
    "uncheck",
    "wait_for",
}
IGNORED_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".output",
    "build",
    "coverage",
    "dist",
    "node_modules",
}
_QUERY_SELECTOR_RE = re.compile(
    r"(?:querySelector|closest|matches)\(\s*['\"]([^'\"]+)['\"]"
)
_GET_BY_ID_RE = re.compile(r"getElementById\(\s*['\"]([^'\"]+)['\"]")
_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+?\s+from\s+)?|require\(\s*)['\"]([^'\"]+)['\"]"
)
_HTML_SRC_RE = re.compile(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_HTML_LINK_RE = re.compile(r"<link\b[^>]*\bhref\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]", re.I)
_ROUTE_COMPONENT_RE = re.compile(
    r"<Route\b[^>]*\bpath\s*=\s*['\"]([^'\"]+)['\"][^>]*"
    r"\belement\s*=\s*\{\s*<([A-Za-z_$][\w$]*)\b",
    re.I | re.S,
)
_DEFAULT_IMPORT_RE = re.compile(
    r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]"
)
_NAMED_IMPORT_RE = re.compile(
    r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", re.S
)
_REGISTER_ROUTE_RE = re.compile(
    r"\bregisterRoute\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z_$][\w$]*)\s*\)"
)
_CSS_CUSTOM_PROPERTY_RE = re.compile(r"(--[A-Za-z_][\w-]*)\s*:")
_CSS_CUSTOM_PROPERTY_USE_RE = re.compile(r"var\(\s*(--[A-Za-z_][\w-]*)")
_MUTATING_SHELL_RE = re.compile(
    r"(?:^|(?:&&|\|\||;)\s*|\s)(?:cp|install|mkdir|mv|patch|rm|tee|touch|truncate)\s|"
    r"(?:^|\s)(?:npm|pnpm|yarn)\s+(?:add|install|create)\b|"
    r"(?:^|\s)(?:perl\s+-pi|sed\s+(?:-[A-Za-z]*i\b|--in-place\b))|"
    r"(?:^|\s)git(?:\s+-C\s+\S+)?\s+(?:apply|checkout|clean|mv|reset|restore|rm|stash)\b|"
    r"(?:^|\s)(?:>>|>)\s*frontend/"
)
_UNSCOPED_EXECUTION_RE = re.compile(
    r"(?:^|(?:&&|\|\||;)\s*)(?:python|python3|npx|uvicorn|vite)\b|"
    r"(?:^|(?:&&|\|\||;)\s*)uv\s+run\s+python\b|"
    r"(?:^|(?:&&|\|\||;)\s*)node\b"
)
_PACKAGE_MANAGER_RE = re.compile(r"(?:^|(?:&&|\|\||;)\s*)(?:npm|pnpm|yarn)\b")


def plan_name(round_num: int) -> str:
    return f"minimal_path_plan_round_{round_num}.json"


def ledger_name(round_num: int) -> str:
    return f"minimal_path_ledger_round_{round_num}.jsonl"


def state_name(round_num: int) -> str:
    return f"minimal_path_state_round_{round_num}.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _walk_strings(value: Any) -> Iterable[tuple[str | None, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield str(key), item
            else:
                yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _target_checks(plan: dict[str, Any], sprint_num: int) -> list[dict[str, Any]]:
    for sprint in plan.get("sprints", []):
        if isinstance(sprint, dict) and sprint.get("sprint") == sprint_num:
            return [item for item in sprint.get("checks", []) if isinstance(item, dict)]
    return []


def _extract_selectors(checks: list[dict[str, Any]]) -> list[str]:
    selectors: set[str] = set()
    for key, value in _walk_strings(checks):
        if key in {"selector", "locator"} and value.strip():
            selectors.add(value.strip())
        for match in _QUERY_SELECTOR_RE.finditer(value):
            selectors.add(match.group(1).strip())
        for match in _GET_BY_ID_RE.finditer(value):
            selectors.add("#" + match.group(1).strip())
    return sorted(item for item in selectors if item)


def _selector_tokens(selectors: list[str]) -> list[str]:
    tokens: set[str] = set(selectors)
    patterns = (
        re.compile(r"#([A-Za-z_][\w:-]*)"),
        re.compile(r"\.([A-Za-z_][\w:-]*)"),
        re.compile(r"\[(?:data-testid|aria-label|name|role)\s*=\s*['\"]?([^'\"\]]+)"),
    )
    for selector in selectors:
        for pattern in patterns:
            tokens.update(
                match.group(1).strip() for match in pattern.finditer(selector)
            )
    return sorted(token for token in tokens if len(token) >= 2)


def _requested_source_roles(checks: list[dict[str, Any]]) -> set[str]:
    """Map executable contract structure to the source layer to inspect first.

    This is deliberately not a natural-language classifier. The harness uses
    typed action/category fields that it already executes, so routing stays
    deterministic and auditable.
    """
    actions = {
        str(action.get("action", "")).strip().lower()
        for check in checks
        for action in check.get("actions", [])
        if isinstance(action, dict)
    }
    categories = {str(check.get("category", "")).strip().lower() for check in checks}
    roles: set[str] = set()
    # Category expresses what the check is judging; the action can merely be
    # the setup needed to reveal that surface. Mixed sprints need the union:
    # dropping behavior merely because one sibling check is visual steers a
    # multi-page editor toward CSS while leaving interactions unimplemented.
    if categories & {"appearance", "responsive", "style", "visual"}:
        roles.add("style")
    behavior_categories = categories & {
        "accessibility",
        "behavior",
        "functional",
        "functionality",
        "interaction",
        "persistence",
        "state",
    }
    if behavior_categories or (
        actions & INTERACTION_ACTIONS
        and not categories & {"appearance", "responsive", "style", "visual"}
    ):
        roles.add("behavior")
    return roles or {"markup"}


def _repair_priority_source_roles(
    harness_dir: Path,
    round_num: int,
    checks: list[dict[str, Any]],
) -> tuple[set[str], list[str]]:
    """Use failed typed checks to rank a repair's first source layer.

    A Sprint can legitimately mix visual and functional checks. Ranking every
    source file from that union lets selector-heavy CSS outrank the JavaScript
    implicated by a failed interaction. The prior grade already contains the
    executable check verdicts, so use those structured IDs without classifying
    evaluator prose or guessing from keywords.
    """
    if round_num <= 1:
        return set(), []
    grade = _read_json(harness_dir / f"grade_round_{round_num - 1}.json", {})
    failed_ids = sorted(
        {
            str(item.get("check_id"))
            for item in grade.get("ui_checks", [])
            if isinstance(item, dict)
            and str(item.get("status", "")).strip().lower()
            in {"error", "fail", "failed", "timeout"}
            and item.get("check_id")
        }
    )
    failed_set = set(failed_ids)
    failed_checks = [
        check for check in checks if str(check.get("id", "")) in failed_set
    ]
    if failed_checks:
        return _requested_source_roles(failed_checks), failed_ids
    edit_guard = grade.get("edit_guard") if isinstance(grade, dict) else None
    violations = edit_guard.get("violations") if isinstance(edit_guard, dict) else None
    if edit_guard and edit_guard.get("passed") is False and isinstance(violations, list) and violations:
        # CSS cannot add/remove semantic DOM/ARIA fragments. When every typed
        # interaction already passed and only the semantic guard failed, route
        # the next repair to behavior/markup source rather than selector-heavy
        # stylesheets. This uses structured guard evidence, not evaluator prose.
        semantic_roles = _requested_source_roles(checks) - {"style"}
        return semantic_roles or {"markup"}, failed_ids
    return set(), failed_ids


def _file_source_roles(path: Path) -> set[str]:
    suffix = path.suffix.lower()
    roles: set[str] = set()
    if suffix in BEHAVIOR_EXTENSIONS:
        roles.add("behavior")
    if suffix in MARKUP_EXTENSIONS:
        roles.add("markup")
    if suffix in STYLE_EXTENSIONS:
        roles.add("style")
    return roles


def _code_files(frontend: Path) -> list[Path]:
    if not frontend.is_dir():
        return []
    return sorted(
        path
        for path in frontend.rglob("*")
        if path.is_file()
        and path.suffix.lower() in CODE_EXTENSIONS
        and not (set(path.relative_to(frontend).parts) & IGNORED_PARTS)
        and path.stat().st_size <= 1_000_000
    )


def _relative_to_workdir(path: Path, workdir: Path) -> str:
    return path.resolve().relative_to(workdir.resolve()).as_posix()


def _source_hotspots(
    files: list[Path],
    selectors: list[str],
    tokens: list[str],
    requested_roles: set[str],
    workdir: Path,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    hotspots: list[dict[str, Any]] = []
    scores: dict[str, int] = {}
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = _relative_to_workdir(path, workdir)
        matches: list[dict[str, Any]] = []
        score = 0
        role_evidence = sorted(_file_source_roles(path) & requested_roles)
        lines = content.splitlines()
        for selector in selectors:
            for index, line in enumerate(lines, start=1):
                if selector in line:
                    matches.append(
                        {"line": index, "anchor": selector, "strength": "selector"}
                    )
                    score += 8
        for token in tokens:
            if token in selectors:
                continue
            for index, line in enumerate(lines, start=1):
                if token in line:
                    matches.append(
                        {"line": index, "anchor": token, "strength": "token"}
                    )
                    score += 3
        if score and role_evidence:
            # The source layer implied by an executable action is stronger
            # evidence than the same selector appearing in incidental text.
            score += 12
        if score:
            scores[relative] = score
            hotspots.append(
                {
                    "path": relative,
                    "score": score,
                    "role_evidence": role_evidence,
                    "matches": matches[:24],
                }
            )
    hotspots.sort(
        key=lambda item: (
            0 if item.get("role_evidence") else 1,
            -int(item["score"]),
            str(item["path"]),
        )
    )
    return hotspots, scores


def _resolve_reference(frontend: Path, source: Path, reference: str) -> Path | None:
    clean = reference.split("?", 1)[0].split("#", 1)[0]
    if not clean or clean.startswith(("http://", "https://", "//", "data:")):
        return None
    if clean.startswith("/"):
        base = frontend / clean.lstrip("/")
    else:
        base = source.parent / clean
    candidates = [base]
    if not base.suffix:
        candidates.extend(base.with_suffix(ext) for ext in sorted(CODE_EXTENSIONS))
        candidates.extend((base / f"index{ext}") for ext in sorted(CODE_EXTENSIONS))
    frontend_resolved = frontend.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(frontend_resolved)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.suffix.lower() in CODE_EXTENSIONS:
            return resolved
    return None


def _dependency_graph(
    files: list[Path], frontend: Path, workdir: Path
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    graph: dict[str, set[str]] = {}
    edges: set[tuple[str, str]] = set()
    for source in files:
        try:
            content = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        references = [match.group(1) for match in _IMPORT_RE.finditer(content)]
        # Navigation anchors connect browser pages, not source modules. Treating
        # <a href> as an import collapses a multi-page site into one giant cone.
        references += [match.group(1) for match in _HTML_SRC_RE.finditer(content)]
        references += [match.group(1) for match in _HTML_LINK_RE.finditer(content)]
        references += [match.group(1) for match in _CSS_IMPORT_RE.finditer(content)]
        source_rel = _relative_to_workdir(source, workdir)
        for reference in references:
            target = _resolve_reference(frontend, source, reference)
            if target is None:
                continue
            target_rel = _relative_to_workdir(target, workdir)
            graph.setdefault(source_rel, set()).add(target_rel)
            edges.add((source_rel, target_rel))
    return graph, [{"from": source, "to": target} for source, target in sorted(edges)]


def _dependency_neighbors(graph: dict[str, set[str]], path: str) -> set[str]:
    """Return direct import/link neighbors in either traversal direction."""
    neighbors = set(graph.get(path, set()))
    neighbors.update(source for source, targets in graph.items() if path in targets)
    return neighbors


def _normalize_route(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    route = value.strip()
    if not route or "://" in route or route.startswith("//"):
        return None
    parsed = urlsplit(route)
    if parsed.query:
        return None
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return None
    normalized = "/" + "/".join(parts)
    normalized = "/" if normalized == "/" else normalized
    if not parsed.fragment:
        return normalized
    fragment = parsed.fragment.rstrip("/")
    fragment_parts = [part for part in fragment.split("/") if part]
    if (
        not fragment.startswith("/")
        or "\\" in fragment
        or "://" in fragment
        or any(part in {".", ".."} for part in fragment_parts)
    ):
        return None
    return f"{normalized}#{fragment}"


def _static_html_route(path: Path, frontend: Path) -> str:
    relative = path.relative_to(frontend).as_posix()
    if relative == "index.html":
        return "/"
    if relative.endswith("/index.html"):
        return "/" + relative.removesuffix("index.html").rstrip("/")
    return "/" + relative


def _file_route(path: Path, frontend: Path) -> str | None:
    """Infer conventional filesystem routes without executing project code."""
    relative = path.relative_to(frontend).as_posix()
    patterns = (
        (re.compile(r"^(?:src/)?app/(.+/)?page\.(?:js|jsx|ts|tsx)$"), "app"),
        (re.compile(r"^(?:src/)?pages/(.+)\.(?:js|jsx|ts|tsx|vue|svelte)$"), "pages"),
    )
    for pattern, kind in patterns:
        match = pattern.match(relative)
        if not match:
            continue
        raw = (match.group(1) or "").strip("/")
        if kind == "app":
            segments = [
                item for item in raw.split("/") if item and not item.startswith("(")
            ]
        else:
            segments = [item for item in raw.split("/") if item]
            if segments and segments[-1].startswith("_"):
                return None
            if segments and segments[-1] == "index":
                segments.pop()
        # A source pattern is not a browser URL. Without a concrete parameter
        # value from a validated check, navigating `/items/:id` would baseline
        # an error page and create false preservation evidence.
        if any(item.startswith("[") and item.endswith("]") for item in segments):
            return None
        route_segments = [
            item for item in segments
        ]
        return "/" + "/".join(route_segments) if route_segments else "/"
    return None


def _route_entries(
    files: list[Path], frontend: Path, workdir: Path
) -> dict[str, set[str]]:
    entries: dict[str, set[str]] = {}
    html_files = [path for path in files if path.suffix.lower() in {".htm", ".html"}]
    static_multi_page = len(html_files) > 1
    for path in files:
        route = (
            _static_html_route(path, frontend)
            if path.suffix.lower() in {".htm", ".html"}
            else None
            if static_multi_page
            else _file_route(path, frontend)
        )
        if route:
            entries.setdefault(route, set()).add(_relative_to_workdir(path, workdir))

    # React Router commonly centralizes route declarations in App/router files.
    # Resolve only explicit literal routes and statically imported components.
    router_entries: dict[str, set[str]] = {}
    if not static_multi_page:
        for router in files:
            try:
                content = router.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            imports: dict[str, str] = {}
            for symbol, reference in _DEFAULT_IMPORT_RE.findall(content):
                target = _resolve_reference(frontend, router, reference)
                if target is not None:
                    imports[symbol] = _relative_to_workdir(target, workdir)
            for raw_symbols, reference in _NAMED_IMPORT_RE.findall(content):
                target = _resolve_reference(frontend, router, reference)
                if target is None:
                    continue
                target_relative = _relative_to_workdir(target, workdir)
                for raw_symbol in raw_symbols.split(","):
                    parts = [item.strip() for item in raw_symbol.strip().split(" as ")]
                    if parts and parts[-1]:
                        imports[parts[-1]] = target_relative
            for route_raw, symbol in _ROUTE_COMPONENT_RE.findall(content):
                route = _normalize_route(route_raw)
                target = imports.get(symbol)
                if route and target:
                    router_entries.setdefault(route, set()).add(target)
            for route_raw, symbol in _REGISTER_ROUTE_RE.findall(content):
                logical = _normalize_route(route_raw)
                target = imports.get(symbol)
                if logical and target:
                    router_entries.setdefault(f"/#{logical}", set()).add(target)
    if router_entries:
        # Explicit router declarations are more authoritative than a Vite
        # index.html shell or filename convention, which otherwise makes every
        # page component look owned by the root route as well.
        entries = router_entries
    return entries


def _dependency_closure(seed_paths: set[str], graph: dict[str, set[str]]) -> set[str]:
    visited: set[str] = set()
    pending = list(seed_paths)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(sorted(graph.get(current, set()) - visited))
    return visited


def _resolve_target_route(route: str, entries: dict[str, set[str]]) -> str | None:
    if route in entries:
        return route
    candidates = []
    if route != "/":
        candidates.extend((route.rstrip("/") + ".html", route.rstrip("/")))
    return next((candidate for candidate in candidates if candidate in entries), None)


def _route_scope(
    *,
    files: list[Path],
    frontend: Path,
    workdir: Path,
    graph: dict[str, set[str]],
    checks: list[dict[str, Any]],
    explicit_target_routes: list[str] | None = None,
    allow_planned_static_routes: bool = False,
) -> dict[str, Any]:
    entries = _route_entries(files, frontend, workdir)
    if not entries:
        fallback_entries = _entrypoints(files, frontend, workdir)
        if fallback_entries:
            entries["/"] = set(fallback_entries[:1])
    check_routes = sorted(
        {
            route
            for route in (_normalize_route(check.get("route", "/")) for check in checks)
            if route
        }
        or {"/"}
    )
    contract_routes = sorted(set(explicit_target_routes or []))
    unexpected_check_routes = sorted(
        set(check_routes) - set(contract_routes)
    ) if contract_routes else []
    # A multi-route Edit should remain route-local per sprint. The explicit
    # contract is the run-wide upper bound, while this sprint opens only the
    # routes for which it has executable checks. If the planner drifts outside
    # that upper bound, keep the user-declared routes in the blocked artifact
    # rather than accidentally legitimizing the drifted page.
    requested = (
        contract_routes if unexpected_check_routes else check_routes
    )
    missing_check_routes: list[str] = []
    resolved = {
        requested_route: _resolve_target_route(requested_route, entries)
        for requested_route in requested
    }
    planned_route_entries: dict[str, str] = {}
    if allow_planned_static_routes and entries:
        for requested_route, match in list(resolved.items()):
            if match is not None or not requested_route.endswith((".htm", ".html")):
                continue
            relative = requested_route.lstrip("/")
            candidate = (frontend / relative).resolve()
            try:
                candidate.relative_to(frontend.resolve())
            except ValueError:
                continue
            if candidate.exists():
                continue
            path = _relative_to_workdir(candidate, workdir)
            entries[requested_route] = {path}
            resolved[requested_route] = requested_route
            planned_route_entries[requested_route] = path
    unresolved = sorted(route for route, match in resolved.items() if match is None)
    target_routes = sorted({match for match in resolved.values() if match is not None})
    closures = {
        route: _dependency_closure(paths, graph) for route, paths in entries.items()
    }
    all_paths = {_relative_to_workdir(path, workdir) for path in files}
    # Framework shells are implicit runtime dependencies rather than normal
    # page imports. Assign them to every page they wrap so a layout/global CSS
    # edit is correctly classified as cross-route shared, not merely unknown.
    for route, entry_paths in entries.items():
        implicit: set[str] = set()
        for entry_path in entry_paths:
            try:
                frontend_rel = Path(entry_path).relative_to("frontend")
            except ValueError:
                continue
            parts = frontend_rel.parts
            normalized_parts = parts[1:] if parts and parts[0] == "src" else parts
            prefix = "frontend/src" if parts and parts[0] == "src" else "frontend"
            if normalized_parts and normalized_parts[0] == "app" and normalized_parts[-1].startswith("page."):
                parent_parts = normalized_parts[:-1]
                for depth in range(1, len(parent_parts) + 1):
                    directory = "/".join(parent_parts[:depth])
                    for extension in ("js", "jsx", "ts", "tsx"):
                        candidate = f"{prefix}/{directory}/layout.{extension}"
                        if candidate in all_paths:
                            implicit.add(candidate)
            if normalized_parts and normalized_parts[0] == "pages":
                for special in ("_app", "_document"):
                    for extension in ("js", "jsx", "ts", "tsx"):
                        candidate = f"{prefix}/pages/{special}.{extension}"
                        if candidate in all_paths:
                            implicit.add(candidate)
        closures[route].update(_dependency_closure(implicit, graph))
    if len(entries) == 1:
        # With no competing page, unreferenced source remains part of that
        # page's editable project surface for backward compatibility.
        only_route = next(iter(entries))
        closures[only_route].update(
            _relative_to_workdir(path, workdir) for path in files
        )
    owners: dict[str, set[str]] = {}
    for route, paths in closures.items():
        for path in paths:
            owners.setdefault(path, set()).add(route)
    target_set = set(target_routes)
    route_local: set[str] = set()
    target_shared: set[str] = set()
    cross_shared: set[str] = set()
    for path, path_owners in owners.items():
        target_owners = path_owners & target_set
        if not target_owners:
            continue
        if path_owners - target_set:
            cross_shared.add(path)
        elif len(path_owners) > 1:
            target_shared.add(path)
        else:
            route_local.add(path)
    off_target = all_paths - route_local - target_shared - cross_shared
    status = (
        "unresolved_target_route"
        if unresolved
        else "target_route_contract_mismatch"
        if unexpected_check_routes or missing_check_routes
        else "multi_page_scoped"
        if len(entries) > 1
        else "single_page"
    )
    return {
        "status": status,
        "requested_routes": requested,
        "check_routes": check_routes,
        "contract_allowed_routes": contract_routes,
        "unexpected_check_routes": unexpected_check_routes,
        "missing_check_routes": missing_check_routes,
        "target_routes": target_routes,
        "protected_routes": sorted(set(entries) - set(target_routes)),
        "unresolved_routes": unresolved,
        "discovered_routes": sorted(entries),
        "route_entries": {
            route: sorted(paths) for route, paths in sorted(entries.items())
        },
        "target_page_entries": sorted(
            {path for route in target_routes for path in entries.get(route, set())}
        ),
        "planned_new_route_entries": sorted(planned_route_entries.values()),
        "planned_new_routes": sorted(planned_route_entries),
        "route_local_paths": sorted(route_local),
        "target_shared_paths": sorted(target_shared),
        "cross_route_shared_paths": sorted(cross_shared),
        "off_target_paths": sorted(off_target),
        "path_owners": {
            path: sorted(path_owners) for path, path_owners in sorted(owners.items())
        },
    }


def _route_symbol_candidates(route: str) -> list[str]:
    """Derive conservative source symbols from one literal browser route.

    This is a mechanical bridge for static multi-page projects that intentionally
    keep route modules in one shared JavaScript file (for example
    ``discovery.html`` -> ``const Discovery = {...}``).  It does not guess product
    semantics: only exact PascalCase forms of literal route path segments qualify.
    """

    parsed = urlsplit(route)
    logical = parsed.fragment if parsed.fragment else parsed.path
    parts = [part for part in logical.strip("/").split("/") if part]
    if not parts:
        return ["Index"]
    parts[-1] = Path(parts[-1]).stem

    def pascal(value: str) -> str:
        return "".join(
            piece[:1].upper() + piece[1:]
            for piece in re.split(r"[^A-Za-z0-9_$]+", value)
            if piece
        )

    candidates = [pascal(parts[-1]), pascal("-".join(parts))]
    return list(dict.fromkeys(item for item in candidates if item))


def _design_system_context(files: list[Path], workdir: Path) -> dict[str, Any]:
    """Index existing CSS custom properties without interpreting product semantics."""

    definitions: dict[str, set[str]] = {}
    usage_counts: dict[str, int] = {}
    style_paths: list[str] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = _relative_to_workdir(path, workdir)
        if path.suffix.lower() in STYLE_EXTENSIONS:
            style_paths.append(relative)
        for name in _CSS_CUSTOM_PROPERTY_RE.findall(content):
            definitions.setdefault(name, set()).add(relative)
        for name in _CSS_CUSTOM_PROPERTY_USE_RE.findall(content):
            usage_counts[name] = usage_counts.get(name, 0) + 1
    tokens = [
        {
            "name": name,
            "defined_in": sorted(paths),
            "usage_count": int(usage_counts.get(name, 0)),
        }
        for name, paths in sorted(definitions.items())
    ][:128]
    return {
        "status": "tokens_found" if tokens else "no_css_custom_properties",
        "css_custom_properties": tokens,
        "style_source_paths": sorted(set(style_paths)),
        "guidance": (
            "Reuse listed custom properties before adding literal colors, spacing, "
            "radii, typography, or timing values; add a token only when the target "
            "semantic cannot be expressed by an existing one."
        ),
    }


def _balanced_brace_end(content: str, opening: int) -> int | None:
    """Return the exclusive end of a balanced JS-like brace block."""

    depth = 0
    state = "code"
    quote = ""
    escaped = False
    index = opening
    while index < len(content):
        char = content[index]
        following = content[index + 1] if index + 1 < len(content) else ""
        if state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and following == "/":
                state = "code"
                index += 1
        elif state == "string":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                state = "code"
        elif char == "/" and following == "/":
            state = "line_comment"
            index += 1
        elif char == "/" and following == "*":
            state = "block_comment"
            index += 1
        elif char in {"'", '"', "`"}:
            state = "string"
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
        index += 1
    return None


def _named_source_region(content: str, symbol: str) -> dict[str, Any] | None:
    """Locate one exact top-level route module and its optional module banner."""

    escaped = re.escape(symbol)
    patterns = (
        (
            "object",
            re.compile(
                rf"(?:\b(?:const|let|var)\s+|\bwindow\.){escaped}\s*=\s*\{{",
                re.M,
            ),
        ),
        (
            "class",
            re.compile(rf"\bclass\s+{escaped}\b[^{{]*\{{", re.M),
        ),
        (
            "function",
            re.compile(
                rf"\b(?:async\s+)?function\s+{escaped}\s*\([^)]*\)\s*\{{",
                re.M,
            ),
        ),
    )
    matches = [
        (kind, match)
        for kind, pattern in patterns
        for match in pattern.finditer(content)
    ]
    if len(matches) != 1:
        return None
    kind, match = matches[0]
    opening = content.find("{", match.start(), match.end())
    end = _balanced_brace_end(content, opening)
    if opening < 0 or end is None:
        return None
    if end < len(content) and content[end] == ";":
        end += 1
    start = match.start()
    prefix = content[:start]
    banner = re.search(
        rf"/\*[^{{}}]{{0,240}}\b{escaped}\b[^{{}}]{{0,240}}\*/\s*$",
        prefix,
        re.I | re.S,
    )
    if banner is not None:
        start = banner.start()
    return {
        "symbol": symbol,
        "kind": kind,
        "start": start,
        "end": end,
        "start_line": content.count("\n", 0, start) + 1,
        "end_line": content.count("\n", 0, end) + 1,
    }


def _guarded_shared_regions(
    *,
    route_scope: dict[str, Any],
    files: list[Path],
    workdir: Path,
    checks: list[dict[str, Any]],
    requested_roles: set[str],
) -> list[dict[str, Any]]:
    """Open only statically named target-route modules in shared source files."""

    by_path = {_relative_to_workdir(path, workdir): path for path in files}
    output: list[dict[str, Any]] = []
    state_intent = any(
        str(check.get("category", "")).lower() in {"persistence", "state"}
        or any(
            isinstance(action, dict)
            and action.get("action")
            in {"assert_storage_value", "set_storage_value"}
            for action in check.get("actions", [])
        )
        for check in checks
    )
    scope_terms: set[str] = set()
    for route in route_scope.get("target_routes") or []:
        for symbol in _route_symbol_candidates(str(route)):
            scope_terms.update(_identifier_terms(symbol))
    for selector in _extract_selectors(checks):
        scope_terms.update(_identifier_terms(selector))
    for check in checks:
        scope_terms.update(_identifier_terms(str(check.get("id", ""))))
    scope_terms -= {
        "action",
        "button",
        "control",
        "controls",
        "data",
        "info",
        "initial",
        "item",
        "items",
        "next",
        "page",
        "pagination",
        "prev",
        "previous",
        "state",
        "storage",
    }
    for relative in route_scope.get("cross_route_shared_paths") or []:
        path = by_path.get(str(relative))
        if path is None or path.suffix.lower() not in BEHAVIOR_EXTENSIONS:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for route in route_scope.get("target_routes") or []:
            for symbol in _route_symbol_candidates(str(route)):
                region = _named_source_region(content, symbol)
                if region is None:
                    continue
                output.append(
                    {
                        "path": str(relative),
                        "route": str(route),
                        "symbol": symbol,
                        "kind": str(region["kind"]),
                        "start_line": int(region["start_line"]),
                        "end_line": int(region["end_line"]),
                    }
                )
                break
        if state_intent and "behavior" in requested_roles:
            for symbol in _state_container_symbols(content):
                region = _named_source_region(content, symbol)
                if region is None:
                    continue
                output.append(
                    {
                        "path": str(relative),
                        "route": "shared-target-state",
                        "symbol": symbol,
                        "kind": str(region["kind"]),
                        "start_line": int(region["start_line"]),
                        "end_line": int(region["end_line"]),
                        "mutation_mode": "additive_target_members",
                        "allowed_terms": sorted(scope_terms),
                    }
                )
    return sorted(output, key=lambda item: (item["path"], item["route"], item["symbol"]))


def _identifier_terms(value: str) -> set[str]:
    pieces = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        item.lower()
        for item in re.split(r"[^A-Za-z0-9]+", pieces)
        if len(item) >= 3
    }


def _state_container_symbols(content: str) -> list[str]:
    symbols = re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{",
        content,
    )
    symbols.extend(re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)\b", content))
    candidates = {
        symbol for symbol in symbols if {"state", "store"} & _identifier_terms(symbol)
    }
    return sorted(candidates)


_ADDITIVE_GENERIC_IDENTIFIERS = {
    "async",
    "const",
    "false",
    "let",
    "null",
    "page",
    "pagesize",
    "return",
    "save",
    "set",
    "state",
    "this",
    "true",
    "var",
}


def _validate_additive_target_members(
    before: str, after: str, allowed_terms: Iterable[str]
) -> str | None:
    """Allow additions whose new identifiers are tied to the selected views.

    This is deliberately lexical and fail-closed. It is an online path guard,
    while browser frames and counterfactual replay remain the acceptance proof.
    """

    terms = {str(item).lower() for item in allowed_terms if str(item).strip()}
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    novel: set[str] = set()
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = before[old_start:old_end]
        new_chunk = after[new_start:new_end]
        old_ids = set(re.findall(r"[A-Za-z_$][\w$]*", old_chunk))
        new_ids = set(re.findall(r"[A-Za-z_$][\w$]*", new_chunk))
        removed = {item for item in old_ids - new_ids if item.lower() not in {"const", "let", "var"}}
        if removed:
            return (
                "Additive shared-state edits cannot remove or replace existing identifiers: "
                + ", ".join(sorted(removed))
            )
        novel.update(item for item in new_ids - old_ids if len(item) >= 3)
    disallowed = sorted(
        token
        for token in novel
        if token.lower() not in _ADDITIVE_GENERIC_IDENTIFIERS
        and not any(term in token.lower() for term in terms)
    )
    if disallowed:
        return (
            "Additive shared-state edits may introduce only target-named members; "
            "unscoped identifiers: "
            + ", ".join(disallowed)
        )
    if novel and not any(
        any(term in token.lower() for term in terms) for token in novel
    ):
        return "Additive shared-state edit contains no target-named member."
    return None


def discover_page_routes(workdir: Path) -> list[str]:
    """Return statically owned browser routes when a project has many pages."""
    frontend = workdir / "frontend"
    files = _code_files(frontend)
    if not files:
        return []
    entries = _route_entries(files, frontend, workdir)
    return sorted(entries) if len(entries) > 1 else []


def _route_isolation_strategy(
    *,
    route_scope: dict[str, Any],
    files: list[Path],
    workdir: Path,
    requested_roles: set[str],
) -> dict[str, Any]:
    """Plan a route-local companion when a static page has no behavior owner.

    This is a guidance decision, not merely a validator exception: an Edit for
    a new interactive page should not wrap or rewrite an already accepted
    page's script when a small page-local module provides a narrower change
    cone.
    """
    if (
        "behavior" not in requested_roles
        or route_scope.get("status") != "multi_page_scoped"
    ):
        return {"status": "not_applicable", "routes": []}
    owners = route_scope.get("path_owners") or {}
    behavior_paths = sorted(
        _relative_to_workdir(path, workdir)
        for path in files
        if path.suffix.lower() in BEHAVIOR_EXTENSIONS
    )
    planned: list[dict[str, str]] = []
    all_existing = {_relative_to_workdir(path, workdir) for path in files}
    for route in route_scope.get("target_routes") or []:
        route_owned_behavior = [
            path
            for path in behavior_paths
            if route in set(owners.get(path) or [])
        ]
        if route_owned_behavior:
            continue
        html_entries = [
            path
            for path in (route_scope.get("route_entries") or {}).get(route, [])
            if Path(path).suffix.lower() in {".htm", ".html"}
        ]
        if not html_entries:
            continue
        entry = sorted(html_entries)[0]
        companion = str(Path(entry).with_suffix(".js")).replace("\\", "/")
        if companion in all_existing:
            continue
        planned.append(
            {
                "route": str(route),
                "entry_path": entry,
                "planned_companion_path": companion,
                **(
                    {"planned_style_path": str(Path(entry).with_suffix(".css")).replace("\\", "/")}
                    if "style" in requested_roles
                    and str(Path(entry).with_suffix(".css")).replace("\\", "/") not in all_existing
                    else {}
                ),
            }
        )
    if not planned:
        return {"status": "not_applicable", "routes": []}
    return {
        "status": "recommended",
        "routes": planned,
        "reason": "interactive static route has no route-owned behavior source",
        "preserve_existing_behavior_sources": behavior_paths,
    }


def _entrypoints(files: list[Path], frontend: Path, workdir: Path) -> list[str]:
    preferred = (
        "index.html",
        "src/App.tsx",
        "src/App.jsx",
        "src/App.vue",
        "src/App.svelte",
        "src/main.tsx",
        "src/main.jsx",
        "src/main.ts",
        "src/main.js",
    )
    existing = {path.resolve(): path for path in files}
    output: list[str] = []
    for name in preferred:
        candidate = (frontend / name).resolve()
        if candidate in existing:
            output.append(_relative_to_workdir(candidate, workdir))
    return output


def _dom_scope(
    baseline: dict[str, Any],
    selectors: list[str],
    tokens: list[str],
    target_routes: list[str] | None = None,
) -> tuple[list[str], bool, list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    target_route_set = set(target_routes or [])
    for root in baseline.get("roots", []):
        if not isinstance(root, dict) or not isinstance(root.get("key"), str):
            continue
        if baseline.get("routes") and root.get("route") not in target_route_set:
            continue
        key = str(root["key"])
        anchors = {
            str(item) for item in root.get("anchors", []) if isinstance(item, str)
        }
        evidence = sorted(
            selector
            for selector in selectors
            if selector in anchors
            or selector.lstrip("#.") == key
            or any(
                token in key or token in anchors
                for token in _selector_tokens([selector])
            )
        )
        if evidence:
            matches.append({"root": key, "evidence": evidence})
    if baseline.get("routes"):
        allowed = []
        route_counts: dict[str, int] = {}
        roots_by_key = {
            str(root.get("key")): root
            for root in baseline.get("roots", [])
            if isinstance(root, dict) and root.get("key")
        }
        for item in matches:
            route = str((roots_by_key.get(item["root"]) or {}).get("route", ""))
            if route_counts.get(route, 0) >= 2:
                continue
            route_counts[route] = route_counts.get(route, 0) + 1
            allowed.append(item["root"])
    else:
        allowed = [item["root"] for item in matches[:2]]
    baseline_has_anchors = any(
        isinstance(root, dict) and bool(root.get("anchors"))
        for root in baseline.get("roots", [])
    )
    # With v2 baselines, an action selector absent from every root is positive
    # evidence that the task introduces a new semantic surface.  Old baselines
    # lack anchor provenance, so preserve the conservative legacy default.
    strong_new_surface_selector = any(
        selector.startswith("#")
        or "data-testid" in selector
        or "aria-label" in selector
        for selector in selectors
    )
    allow_new = bool(
        selectors
        and baseline_has_anchors
        and not allowed
        and strong_new_surface_selector
    )
    return allowed, allow_new, matches


def _selector_compounds(selector: str) -> list[str]:
    """Split a CSS selector on top-level combinators without parsing attributes."""
    compounds: list[str] = []
    current: list[str] = []
    bracket_depth = 0
    paren_depth = 0
    quote: str | None = None
    for char in selector.strip():
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == "[":
            bracket_depth += 1
            current.append(char)
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
            current.append(char)
        elif char == "(":
            paren_depth += 1
            current.append(char)
        elif char == ")":
            paren_depth = max(0, paren_depth - 1)
            current.append(char)
        elif bracket_depth == 0 and paren_depth == 0 and (
            char.isspace() or char in {">", "+", "~"}
        ):
            value = "".join(current).strip()
            if value:
                compounds.append(value)
            current = []
        else:
            current.append(char)
    value = "".join(current).strip()
    if value:
        compounds.append(value)
    return compounds


def _selector_anchor_variants(selector: str) -> set[str]:
    """Return stable leaf-anchor forms used by semantic DOM snapshots."""
    normalized = re.sub(
        r"=\s*'([^']*)'", lambda match: f'="{match.group(1)}"', selector.strip()
    )
    variants = {selector.strip(), normalized}
    for value in (selector.strip(), normalized):
        compounds = _selector_compounds(value)
        if compounds:
            variants.add(compounds[-1])
    return {item for item in variants if item}


def _selector_base(selector: str) -> str:
    compounds = _selector_compounds(selector)
    value = compounds[0] if compounds else selector
    return re.sub(r":{1,2}[A-Za-z_-][\w-]*(?:\([^)]*\))?", "", value)


def _routed_selector_contracts(check: dict[str, Any]) -> list[dict[str, Any]]:
    """Bind selectors to the route active at their typed-action position."""
    initial_route = _normalize_route(check.get("route", "/"))
    if initial_route is None:
        return []
    current_route = initial_route
    contracts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in check.get("actions", []):
        if not isinstance(action, dict):
            continue
        for selector in _extract_selectors([action]):
            count = action.get("count") if action.get("action") == "assert_count" else 1
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                count = 1
            contracts.append(
                {
                    "route": current_route,
                    "selector": selector,
                    "max_count": min(count, 50),
                }
            )
            seen.add(selector)
        if action.get("action") == "assert_url":
            next_route = _normalize_route(action.get("value", ""))
            if next_route is not None:
                current_route = next_route
    # Preserve support for selectors carried by older check-level fields while
    # keeping typed action selectors route-aware.
    for selector in _extract_selectors([check]):
        if selector not in seen:
            contracts.append(
                {"route": initial_route, "selector": selector, "max_count": 1}
            )
    return contracts


def _fragment_dom_scope(
    baseline: dict[str, Any],
    checks: list[dict[str, Any]],
    target_routes: list[str],
    *,
    existing_source_selectors: set[str] | None = None,
    expected_new_routes: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Resolve each action selector to the deepest stable semantic fragment."""
    if baseline.get("version") != 4:
        return [], [], [], []
    fragments = {
        str(item["key"]): item
        for item in baseline.get("fragments", [])
        if isinstance(item, dict) and item.get("key")
    }

    def depth(key: str) -> int:
        value = 0
        parent = (fragments.get(key) or {}).get("parent_key")
        visited = {key}
        while isinstance(parent, str) and parent and parent not in visited:
            visited.add(parent)
            value += 1
            parent = (fragments.get(parent) or {}).get("parent_key")
        return value

    allowed: list[str] = []
    expected_new: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    unresolved: list[str] = []
    target_set = set(target_routes)
    source_selectors = existing_source_selectors or set()
    new_route_set = expected_new_routes or set()
    routed: dict[tuple[str, str], dict[str, Any]] = {}
    for check in checks:
        for contract in _routed_selector_contracts(check):
            route = str(contract["route"])
            if route not in target_set:
                continue
            key = (route, str(contract["selector"]))
            prior = routed.get(key)
            if prior is None or int(contract["max_count"]) > int(prior["max_count"]):
                routed[key] = contract

    pending_new: list[dict[str, Any]] = []
    source_variants = {
        variant
        for selector in source_selectors
        for variant in _selector_anchor_variants(selector)
    }
    for contract in routed.values():
        route = str(contract["route"])
        selector = str(contract["selector"])
        variants = _selector_anchor_variants(selector)
        candidates = [
            key
            for key, item in fragments.items()
            if str(item.get("route", "/")) == route
            and bool(
                variants
                & {
                    str(anchor)
                    for anchor in item.get("anchors", [])
                    if isinstance(anchor, str)
                }
            )
        ]
        if candidates:
            selected = max(candidates, key=lambda key: (depth(key), key))
            if selected not in allowed:
                allowed.append(selected)
            evidence.append(
                {
                    "selector": selector,
                    "route": route,
                    "fragment": selected,
                    "candidate_count": len(candidates),
                    "resolution": "baseline_fragment",
                }
            )
            continue
        if variants & source_variants:
            evidence.append(
                {
                    "selector": selector,
                    "route": route,
                    "resolution": "runtime_state_source_anchor",
                }
            )
            continue
        if route in new_route_set:
            evidence.append(
                {
                    "selector": selector,
                    "route": route,
                    "resolution": "covered_by_expected_new_route",
                }
            )
            continue
        terminal = (_selector_compounds(selector) or [selector])[-1]
        strong = bool(
            re.search(r"(?:^|[^\\])(?:#|\.)[A-Za-z_-]", terminal)
            or terminal.startswith("[data-testid=")
            or terminal.startswith("[aria-label=")
        )
        if strong:
            pending_new.append(contract)
        else:
            unresolved.append(f"{route}::{selector}")

    for contract in pending_new:
        selector = str(contract["selector"])
        compounds = _selector_compounds(selector)
        covering = next(
            (
                other
                for other in pending_new
                if other is not contract
                and other["route"] == contract["route"]
                and len(compounds) > 1
                and _selector_base(str(other["selector"]))
                == _selector_base(compounds[0])
            ),
            None,
        )
        if covering is not None:
            evidence.append(
                {
                    "selector": selector,
                    "route": contract["route"],
                    "resolution": "covered_by_expected_subtree",
                    "covering_selector": covering["selector"],
                }
            )
            continue
        expected_new.append(contract)
    return allowed, expected_new, evidence, sorted(set(unresolved))


def _scope_payload(plan: dict[str, Any]) -> dict[str, Any]:
    dom = plan["dom_change_cone"]
    return {
        "schema_version": "edit-scope-v4",
        "owner": "harness",
        "plan": f".harness/{plan_name(int(plan['round']))}",
        "baseline": dom.get("baseline"),
        "allowed_root_keys": list(dom.get("allowed_root_keys", [])),
        "allow_new_roots": bool(dom.get("allow_new_roots", False)),
        "allowed_fragment_keys": list(dom.get("allowed_fragment_keys", [])),
        "expected_new_fragments": list(dom.get("expected_new_fragments", [])),
        "expected_new_routes": list(dom.get("expected_new_routes", [])),
        "target_routes": list((plan.get("route_scope") or {}).get("target_routes", [])),
        "protected_routes": list((plan.get("route_scope") or {}).get("protected_routes", [])),
    }


def ensure_minimal_path_plan(
    *,
    workdir: Path,
    harness_dir: Path,
    round_num: int,
    sprint_num: int,
    mode: str,
    max_patch_lines: int,
    max_touched_files: int,
) -> dict[str, Any]:
    """Create one immutable harness-owned plan for an edit/repair round."""
    path = harness_dir / plan_name(round_num)
    existing = _read_json(path, None)
    if (
        isinstance(existing, dict)
        and existing.get("schema_version") in SUPPORTED_PLAN_VERSIONS
    ):
        scope_path = harness_dir / f"edit_scope_round_{round_num}.json"
        if not scope_path.exists():
            _write_json(scope_path, _scope_payload(existing))
        return existing

    ui_plan = _read_json(harness_dir / "ui_verification_plan.json", {})
    checks = _target_checks(ui_plan, sprint_num)
    selectors = _extract_selectors(checks)
    tokens = _selector_tokens(selectors)
    requested_roles = _requested_source_roles(checks)
    priority_roles, failed_check_ids = (
        _repair_priority_source_roles(harness_dir, round_num, checks)
        if mode == "repair"
        else (set(), [])
    )
    requested_roles |= priority_roles
    baseline_candidates = (
        harness_dir / f"repair_dom_source_round_{round_num}.json",
        harness_dir / f"edit_dom_source_sprint_{sprint_num}.json",
        harness_dir / "edit_dom_baseline.json",
    )
    baseline_path = next((item for item in baseline_candidates if item.is_file()), None)
    baseline = _read_json(baseline_path, {}) if baseline_path else {}
    frontend = workdir / "frontend"
    files = _code_files(frontend)
    all_hotspots, all_scores = _source_hotspots(
        files, selectors, tokens, requested_roles, workdir
    )
    graph, edges = _dependency_graph(files, frontend, workdir)
    entries = _entrypoints(files, frontend, workdir)
    edit_task_contract = _read_json(harness_dir / "edit_task_contract.json", {})
    explicit_target_routes = edit_task_contract.get("requested_target_routes")
    if not isinstance(explicit_target_routes, list):
        explicit_target_routes = []
    route_scope = _route_scope(
        files=files,
        frontend=frontend,
        workdir=workdir,
        graph=graph,
        checks=checks,
        explicit_target_routes=[str(item) for item in explicit_target_routes],
        allow_planned_static_routes=(
            mode == "generate" and not (workdir / "seed_manifest.json").is_file()
        ),
    )
    isolation_strategy = _route_isolation_strategy(
        route_scope=route_scope,
        files=files,
        workdir=workdir,
        requested_roles=requested_roles,
    )
    allowed_roots, allow_new_roots, root_evidence = _dom_scope(
        baseline,
        selectors,
        tokens,
        list(route_scope.get("target_routes") or []),
    )
    allowed_fragments, expected_new_fragments, fragment_evidence, unresolved_fragments = (
        _fragment_dom_scope(
            baseline,
            checks,
            list(route_scope.get("target_routes") or []),
            existing_source_selectors={
                str(match.get("anchor"))
                for hotspot in all_hotspots
                for match in hotspot.get("matches", [])
                if isinstance(match, dict)
                and match.get("strength") == "selector"
                and match.get("anchor")
            },
            expected_new_routes=set(route_scope.get("planned_new_routes") or []),
        )
    )
    planned_companions = {
        str(path)
        for item in isolation_strategy.get("routes", [])
        if isinstance(item, dict)
        for path in (item.get("planned_companion_path"), item.get("planned_style_path"))
        if path
    }
    isolation_entries = {
        str(item["entry_path"])
        for item in isolation_strategy.get("routes", [])
        if isinstance(item, dict) and item.get("entry_path")
    }
    planned_route_entries = set(route_scope.get("planned_new_route_entries") or [])
    guarded_shared_regions = _guarded_shared_regions(
        route_scope=route_scope,
        files=files,
        workdir=workdir,
        checks=checks,
        requested_roles=requested_roles,
    )
    guarded_shared_paths = {
        str(item["path"]) for item in guarded_shared_regions
    }
    admissible_paths = (
        set(route_scope["route_local_paths"])
        | set(route_scope["target_shared_paths"])
        | guarded_shared_paths
        | planned_companions
    )
    hotspots = [
        item for item in all_hotspots if str(item.get("path")) in admissible_paths
    ]
    if priority_roles:
        hotspots.sort(
            key=lambda item: (
                0 if set(item.get("role_evidence") or []) & priority_roles else 1,
                0 if item.get("role_evidence") else 1,
                -int(item["score"]),
                str(item["path"]),
            )
        )
    scores = {
        path: score for path, score in all_scores.items() if path in admissible_paths
    }

    ranked = [str(item["path"]) for item in hotspots]
    target_entries = list(route_scope["target_page_entries"])
    if isolation_strategy.get("status") == "recommended":
        # The page entry is the first small mutation: connect it to the planned
        # route-local companion. Existing behavior sources remain protected.
        initial = sorted(isolation_entries)[:1]
        local = sorted(isolation_entries)
    else:
        if len(route_scope.get("target_routes") or []) > 1:
            owners = route_scope.get("path_owners") or {}
            per_route_initial: list[str] = []
            for route in route_scope.get("target_routes") or []:
                candidate = next(
                    (
                        path
                        for path in ranked
                        if route in set(owners.get(path) or [])
                    ),
                    None,
                )
                if candidate is None:
                    candidate = next(
                        (
                            path
                            for path in target_entries
                            if route in set(owners.get(path) or [])
                        ),
                        None,
                    )
                if candidate and candidate not in per_route_initial:
                    per_route_initial.append(candidate)
            initial = per_route_initial or (ranked[:1] if ranked else target_entries[:1])
        else:
            initial = ranked[:1] if ranked else target_entries[:1]
        seeds = ranked[:max_touched_files]
        if not seeds:
            seeds = target_entries[:1]
        local = list(dict.fromkeys(seeds))
        # A fallback entry point is useful only together with its direct imports;
        # that is the smallest executable source unit for common static/SPA seeds.
        if not ranked:
            for seed in list(local):
                for dependency in sorted(_dependency_neighbors(graph, seed)):
                    if (
                        dependency in admissible_paths
                        and dependency not in local
                        and len(local) < max_touched_files
                    ):
                        local.append(dependency)
    # The hotspot table retains rank/score evidence.  The executable allowlist
    # is sorted so repeated runs expose a stable path order to models/tools.
    local = sorted(local)

    dependencies: list[str] = sorted(planned_companions)
    for seed in local:
        for dependency in sorted(_dependency_neighbors(graph, seed)):
            if (
                dependency in admissible_paths
                and dependency not in local
                and dependency not in dependencies
            ):
                dependencies.append(dependency)
    all_paths = [_relative_to_workdir(item, workdir) for item in files]
    protected = sorted(set(all_paths) - set(local) - set(dependencies))
    executable_edges = [
        edge
        for edge in edges
        if edge["from"] in admissible_paths
        and edge["to"] in admissible_paths
        and (
            isolation_strategy.get("status") != "recommended"
            or (
                edge["from"] in set(local) | set(dependencies)
                and edge["to"] in set(local) | set(dependencies)
            )
        )
    ]
    executable_edges.extend(
        {
            "from": str(item["entry_path"]),
            "to": str(path),
            "kind": (
                "planned_route_style"
                if path == item.get("planned_style_path")
                else "planned_route_companion"
            ),
        }
        for item in isolation_strategy.get("routes", [])
        if isinstance(item, dict)
        for path in (item.get("planned_companion_path"), item.get("planned_style_path"))
        if path
    )
    fragment_routes = {
        str(item.get("key")): str(item.get("route", "/"))
        for item in baseline.get("fragments", [])
        if isinstance(item, dict) and item.get("key")
    }
    fragment_route_counts = {
        route: sum(
            1 for key in allowed_fragments if fragment_routes.get(key) == route
        )
        + sum(
            1
            for contract in expected_new_fragments
            if contract.get("route") == route
        )
        for route in route_scope.get("target_routes", [])
    }
    status = (
        "blocked"
        if route_scope["unresolved_routes"]
        or route_scope["unexpected_check_routes"]
        or route_scope["missing_check_routes"]
        or bool(unresolved_fragments)
        or any(count > 4 for count in fragment_route_counts.values())
        else "ready"
        if local and checks
        else "advisory"
    )
    plan = {
        "schema_version": PLAN_VERSION,
        "owner": "harness",
        "round": round_num,
        "sprint": sprint_num,
        "mode": mode,
        "status": status,
        "route_scope": route_scope,
        "target_contract": {
            "check_ids": [str(item.get("id", "")) for item in checks],
            "selectors": selectors,
            "selector_tokens": tokens,
            "requested_source_roles": sorted(requested_roles),
            "priority_source_roles": sorted(priority_roles),
            "failed_check_ids": failed_check_ids,
        },
        "design_system_context": _design_system_context(files, workdir),
        "dom_change_cone": {
            "baseline": (
                f".harness/{baseline_path.name}" if baseline_path is not None else None
            ),
            "allowed_root_keys": allowed_roots,
            "allow_new_roots": allow_new_roots,
            "root_evidence": root_evidence,
            "allowed_fragment_keys": allowed_fragments,
            "expected_new_fragments": expected_new_fragments,
            "expected_new_routes": list(route_scope.get("planned_new_routes") or []),
            "fragment_evidence": fragment_evidence,
            "unresolved_fragment_selectors": unresolved_fragments,
        },
        "source_change_cone": {
            "local_paths": local,
            "initial_paths": initial,
            "dependency_paths": dependencies,
            "planned_new_paths": sorted(planned_companions | planned_route_entries),
            "protected_paths": protected,
            "dependency_edges": executable_edges,
            "guarded_shared_regions": guarded_shared_regions,
            "hotspots": [
                item
                for item in hotspots
                if isolation_strategy.get("status") != "recommended"
                or str(item.get("path")) in set(local) | set(dependencies)
            ],
            "entrypoints": entries,
            "path_scores": scores,
            "route_isolation_strategy": isolation_strategy,
        },
        "budgets": {
            "max_patch_lines": max(1, int(max_patch_lines)),
            "max_touched_files": max(1, int(max_touched_files)),
            "existing_source_requires_exact_patch": True,
        },
        "widening_policy": {
            "dependency_tier": (
                "only_after_source_mutation_and_post_mutation_validation_attempt"
            ),
            "protected_tier": "denied",
            "whole_file_overwrite": "denied_for_existing_frontend_source",
            "inspection_before_mutation": "required",
            "commit_after_successful_validation": "required",
        },
    }
    _write_json(path, plan)
    _write_json(
        harness_dir / f"edit_scope_round_{round_num}.json", _scope_payload(plan)
    )
    return plan


class MinimalPathPolicy:
    """Stateful pre-mutation gate backed by a harness-owned plan."""

    def __init__(self, workdir: Path, plan: dict[str, Any]) -> None:
        self.workdir = workdir.resolve()
        self.plan = plan
        self.round_num = int(plan["round"])
        cone = plan.get("source_change_cone") or {}
        self.local_paths = set(cone.get("local_paths") or [])
        configured_initial = set(cone.get("initial_paths") or [])
        hotspots = [
            str(item.get("path"))
            for item in cone.get("hotspots") or []
            if isinstance(item, dict) and item.get("path")
        ]
        fallback_initial = hotspots[:1] or sorted(self.local_paths)[:1]
        self.initial_paths = configured_initial or set(fallback_initial)
        self.dependency_paths = set(cone.get("dependency_paths") or [])
        self.protected_paths = set(cone.get("protected_paths") or [])
        route_scope = plan.get("route_scope") or {}
        self.cross_route_shared_paths = set(
            route_scope.get("cross_route_shared_paths") or []
        )
        self.guarded_shared_regions: dict[str, list[dict[str, Any]]] = {}
        for item in cone.get("guarded_shared_regions") or []:
            if isinstance(item, dict) and item.get("path") and item.get("symbol"):
                self.guarded_shared_regions.setdefault(str(item["path"]), []).append(item)
        self.off_target_paths = set(route_scope.get("off_target_paths") or [])
        self.dependency_edges = {
            (str(item.get("from")), str(item.get("to")))
            for item in cone.get("dependency_edges") or []
            if isinstance(item, dict)
        }
        budgets = plan.get("budgets") or {}
        self.max_patch_lines = max(1, int(budgets.get("max_patch_lines", 120)))
        self.max_touched_files = max(1, int(budgets.get("max_touched_files", 3)))
        self.state_path = self.workdir / ".harness" / state_name(self.round_num)
        state = _read_json(self.state_path, {})
        self.observed_paths: set[str] = set(state.get("observed_paths") or [])
        self.touched_paths: set[str] = set(state.get("touched_paths") or [])
        self.mutation_revision = int(state.get("mutation_revision") or 0)
        self.validation_attempt_revision = int(
            state.get("validation_attempt_revision") or 0
        )
        self.validation_success_revision = int(
            state.get("validation_success_revision") or 0
        )
        self.validation_last_ok = state.get("validation_last_ok")
        self.ledger_path = self.workdir / ".harness" / ledger_name(self.round_num)
        self._persist_state()

    @classmethod
    def from_plan(cls, workdir: Path, plan: dict[str, Any]) -> "MinimalPathPolicy":
        return cls(workdir, plan)

    @classmethod
    def load(cls, workdir: Path, round_num: int) -> "MinimalPathPolicy | None":
        plan = _read_json(workdir / ".harness" / plan_name(round_num), None)
        if (
            not isinstance(plan, dict)
            or plan.get("schema_version") not in SUPPORTED_PLAN_VERSIONS
        ):
            return None
        return cls(workdir, plan)

    def _record(
        self,
        *,
        decision: str,
        tool: str,
        path: str | None,
        reason: str,
        scope_tier: str | None = None,
        patch_lines: int | None = None,
        expansion_reason: str | None = None,
    ) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        existing = 0
        if self.ledger_path.is_file():
            with self.ledger_path.open(encoding="utf-8") as handle:
                existing = sum(1 for _ in handle)
        payload: dict[str, Any] = {
            "sequence": existing + 1,
            "round": self.round_num,
            "decision": decision,
            "tool": tool,
            "path": path,
            "reason": reason,
        }
        if scope_tier is not None:
            payload["scope_tier"] = scope_tier
        if patch_lines is not None:
            payload["patch_lines"] = patch_lines
        if expansion_reason is not None:
            payload["expansion_reason"] = expansion_reason
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()

    def _persist_state(self) -> None:
        unlocked_paths = set(self.initial_paths) | set(self.touched_paths)
        if self.validation_attempt_revision >= self.mutation_revision:
            for source, target in self.dependency_edges:
                if source in self.touched_paths:
                    unlocked_paths.add(target)
                if target in self.touched_paths:
                    unlocked_paths.add(source)
        if not (self.initial_paths & self.observed_paths):
            phase = "inspect_initial"
            next_action = "Read the exact initial source path before proposing a patch."
        elif self.mutation_revision == 0:
            phase = "patch_initial"
            next_action = "Apply one exact unique patch to the inspected initial path."
        elif self.validation_attempt_revision < self.mutation_revision:
            phase = "validate_latest_patch"
            next_action = (
                "Run the smallest applicable syntax, build, test, or diff validation."
            )
        elif (
            self.validation_success_revision < self.mutation_revision
            or self.validation_last_ok is not True
        ):
            phase = "repair_or_expand"
            next_action = "Use the failure evidence to repair the touched path or follow one unlocked dependency edge."
        else:
            phase = "validated"
            next_action = "Commit if the target contract is complete; widen only when a recorded dependency is still necessary."
        _write_json(
            self.state_path,
            {
                "schema_version": "minimal-path-state-v1",
                "owner": "harness",
                "round": self.round_num,
                "observed_paths": sorted(self.observed_paths),
                "touched_paths": sorted(self.touched_paths),
                "mutation_revision": self.mutation_revision,
                "validation_attempt_revision": self.validation_attempt_revision,
                "validation_success_revision": self.validation_success_revision,
                "validation_last_ok": self.validation_last_ok,
                "phase": phase,
                "unlocked_paths": sorted(unlocked_paths),
                "next_action": next_action,
            },
        )

    def _path(self, tool_input: dict[str, Any]) -> tuple[Path, str] | None:
        raw = tool_input.get("path") or tool_input.get("file_path")
        if not isinstance(raw, str) or not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.workdir).as_posix()
        except ValueError:
            return None
        return resolved, relative

    @staticmethod
    def _patch_pairs(tool: str, tool_input: dict[str, Any]) -> list[tuple[str, str]]:
        normalized = tool.lower()
        if normalized in {"write", "write_file"}:
            return [("", str(tool_input.get("content", "")))]
        if normalized in {"apply_patch", "edit"}:
            old = tool_input.get("old_text", tool_input.get("old_string", ""))
            new = tool_input.get("new_text", tool_input.get("new_string", ""))
            return [(str(old), str(new))]
        if normalized == "multiedit":
            output: list[tuple[str, str]] = []
            for item in tool_input.get("edits") or []:
                if isinstance(item, dict):
                    output.append(
                        (
                            str(item.get("old_string", item.get("old_text", ""))),
                            str(item.get("new_string", item.get("new_text", ""))),
                        )
                    )
            return output
        return []

    def _deny(
        self,
        tool: str,
        path: str | None,
        reason: str,
        *,
        patch_lines: int | None = None,
    ) -> str:
        self._record(
            decision="deny",
            tool=tool,
            path=path,
            reason=reason,
            patch_lines=patch_lines,
        )
        return reason

    @staticmethod
    def _is_validation_command(command: str) -> bool:
        normalized = " ".join(command.strip().split()).lower()
        return bool(
            re.search(
                r"(?:^|&&\s*)(?:"
                r"node --check\b|"
                r"(?:npm|pnpm|yarn)(?:\s+--prefix\s+\S+)?\s+(?:run\s+)?(?:build|check|lint|test|typecheck|validate)\b|"
                r"git(?:\s+-c\s+\S+)?\s+diff\s+--check\b|"
                r"pytest\b|uv run pytest\b|tsc\b"
                r")",
                normalized,
            )
        )

    @staticmethod
    def _is_commit_command(command: str) -> bool:
        normalized = " ".join(command.strip().split()).lower()
        return bool(
            re.search(
                r"(?:^|&&\s*)git(?:\s+-c\s+\S+)?\s+commit\b",
                normalized,
            )
        )

    @staticmethod
    def _is_readonly_package_command(command: str) -> bool:
        normalized = " ".join(command.strip().split()).lower()
        matches = list(_PACKAGE_MANAGER_RE.finditer(normalized))
        return bool(matches) and all(
            re.match(
                r"(?:npm|pnpm|yarn)(?:\s+--prefix\s+\S+)?\s+(?:info|list|ls|outdated|view)\b",
                normalized[match.start() :],
            )
            for match in matches
        )

    def _dependency_predecessor(self, relative: str) -> str | None:
        return next(
            (
                source if target == relative else target
                for source, target in sorted(self.dependency_edges)
                if (target == relative and source in self.touched_paths)
                or (source == relative and target in self.touched_paths)
            ),
            None,
        )

    def _current_guarded_spans(
        self, relative: str, content: str
    ) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for contract in self.guarded_shared_regions.get(relative, []):
            region = _named_source_region(content, str(contract.get("symbol", "")))
            if region is not None and str(region.get("kind")) == str(contract.get("kind")):
                spans.append((int(region["start"]), int(region["end"])))
        return spans

    def _guarded_contract_for_span(
        self, relative: str, content: str, start: int, end: int
    ) -> dict[str, Any] | None:
        for contract in self.guarded_shared_regions.get(relative, []):
            region = _named_source_region(content, str(contract.get("symbol", "")))
            if (
                region is not None
                and str(region.get("kind")) == str(contract.get("kind"))
                and int(region["start"]) <= start
                and end <= int(region["end"])
            ):
                return contract
        return None

    def _guarded_projection(
        self, relative: str, content: str
    ) -> tuple[str | None, str | None]:
        contracts = self.guarded_shared_regions.get(relative, [])
        regions: list[tuple[int, int, str, str]] = []
        for contract in contracts:
            symbol = str(contract.get("symbol", ""))
            kind = str(contract.get("kind", ""))
            region = _named_source_region(content, symbol)
            if region is None or str(region.get("kind")) != kind:
                return None, (
                    f"Guarded target-route region {symbol!r} cannot be resolved "
                    f"uniquely in {relative}."
                )
            regions.append(
                (int(region["start"]), int(region["end"]), symbol, kind)
            )
        regions.sort()
        if any(left[1] > right[0] for left, right in zip(regions, regions[1:])):
            return None, f"Guarded target-route regions overlap in {relative}."
        parts: list[str] = []
        cursor = 0
        for start, end, symbol, kind in regions:
            parts.append(content[cursor:start])
            parts.append(f"\0HARNESS_GUARDED_REGION:{kind}:{symbol}\0")
            cursor = end
        parts.append(content[cursor:])
        return "".join(parts), None

    def validate_guarded_shared_file(
        self, relative: str, *, before: str, after: str
    ) -> str | None:
        """Require every byte outside approved shared-file regions to stay fixed."""

        if relative not in self.guarded_shared_regions:
            return f"No guarded target-route region is authorized for {relative}."
        before_projection, before_error = self._guarded_projection(relative, before)
        if before_error:
            return before_error
        after_projection, after_error = self._guarded_projection(relative, after)
        if after_error:
            return after_error
        if before_projection != after_projection:
            return (
                f"Committed Edit changed bytes outside the guarded target-route "
                f"region in {relative}."
            )
        for contract in self.guarded_shared_regions.get(relative, []):
            if contract.get("mutation_mode") != "additive_target_members":
                continue
            before_region = _named_source_region(
                before, str(contract.get("symbol", ""))
            )
            after_region = _named_source_region(
                after, str(contract.get("symbol", ""))
            )
            if before_region is None or after_region is None:
                return (
                    "Additive shared-state region cannot be resolved after the Edit: "
                    + str(contract.get("symbol", ""))
                )
            error = _validate_additive_target_members(
                before[int(before_region["start"]) : int(before_region["end"])],
                after[int(after_region["start"]) : int(after_region["end"])],
                contract.get("allowed_terms") or [],
            )
            if error:
                return error
        return None

    def observe_result(
        self,
        tool: str,
        tool_input: dict[str, Any],
        *,
        ok: bool,
        output: Any,
    ) -> None:
        """Advance the controller only from an observed tool result.

        Permission is a precondition, not evidence that the action happened.
        Reads, mutations, and validations therefore update state only here.
        """
        normalized = tool.lower()
        resolved_path = self._path(tool_input)
        if normalized in {"read", "read_file"} and ok and resolved_path is not None:
            _absolute, relative = resolved_path
            if relative.startswith("frontend/"):
                self.observed_paths.add(relative)
                self._record(
                    decision="observe",
                    tool=tool,
                    path=relative,
                    reason="source inspection completed before mutation",
                )
                self._persist_state()
            return

        mutation_tools = {"write", "write_file", "edit", "apply_patch", "multiedit"}
        if normalized in mutation_tools and resolved_path is not None:
            _absolute, relative = resolved_path
            if ok and relative.startswith("frontend/"):
                self.touched_paths.add(relative)
                self.mutation_revision += 1
                self._record(
                    decision="applied",
                    tool=tool,
                    path=relative,
                    reason="authorized mutation completed",
                )
                self._persist_state()
            elif not ok:
                self._record(
                    decision="failed",
                    tool=tool,
                    path=relative,
                    reason=str(output)[:500] or "mutation failed",
                )
            return

        if normalized in {"bash", "run_command"}:
            command = str(tool_input.get("command", ""))
            if self._is_validation_command(command):
                self.observe_validation(ok=ok, output=output, tool=tool)

    def observe_validation(self, *, ok: bool, output: Any, tool: str) -> None:
        """Record a real harness or agent validation checkpoint."""
        self.validation_attempt_revision = self.mutation_revision
        self.validation_last_ok = ok
        if ok:
            self.validation_success_revision = self.mutation_revision
        self._record(
            decision="validation_pass" if ok else "validation_fail",
            tool=tool,
            path=None,
            reason=("post-mutation validation passed" if ok else str(output)[:500]),
        )
        self._persist_state()

    def check(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        """Return a denial message, or ``None`` when the mutation is admissible."""
        normalized = tool.lower()
        if normalized in {"bash", "run_command"}:
            command = str(tool_input.get("command", ""))
            validation_command = self._is_validation_command(command)
            if _MUTATING_SHELL_RE.search(command):
                return self._deny(
                    tool,
                    None,
                    "Minimal-path mode routes source changes through mutation tools; "
                    "filesystem/package mutations through Bash are denied.",
                )
            if _UNSCOPED_EXECUTION_RE.search(command) and not validation_command:
                return self._deny(
                    tool,
                    None,
                    "Minimal-path mode denies arbitrary interpreter or script execution; "
                    "use read-only diagnosis, an explicit validation command, or mutation tools.",
                )
            if (
                _PACKAGE_MANAGER_RE.search(command)
                and not validation_command
                and not self._is_readonly_package_command(command)
            ):
                return self._deny(
                    tool,
                    None,
                    "Minimal-path mode allows package managers only for read-only inspection "
                    "or explicit build/test/lint/check validation.",
                )
            if (
                self._is_commit_command(command)
                and self.mutation_revision > 0
                and (
                    self.mutation_revision > self.validation_success_revision
                    or self.validation_last_ok is not True
                )
            ):
                return self._deny(
                    tool,
                    None,
                    "A successful validation after the latest source mutation is required "
                    "before commit.",
                )
            return None

        mutation_tools = {"write", "write_file", "edit", "apply_patch", "multiedit"}
        if normalized not in mutation_tools:
            return None
        resolved_path = self._path(tool_input)
        if resolved_path is None:
            return None
        absolute, relative = resolved_path

        owned_artifacts = {
            f".harness/{plan_name(self.round_num)}",
            f".harness/edit_scope_round_{self.round_num}.json",
            f".harness/{ledger_name(self.round_num)}",
            f".harness/{state_name(self.round_num)}",
        }
        if relative in owned_artifacts:
            return self._deny(
                tool,
                relative,
                "This scope artifact is harness-owned and cannot be changed by the model.",
            )
        if not relative.startswith("frontend/"):
            return None
        if absolute.suffix.lower() not in CODE_EXTENSIONS:
            return None

        guarded_shared = relative in self.cross_route_shared_paths
        if guarded_shared and relative not in self.guarded_shared_regions:
            owners = (
                (self.plan.get("route_scope") or {})
                .get("path_owners", {})
                .get(relative, [])
            )
            return self._deny(
                tool,
                relative,
                "This source is shared with non-target routes and has no mechanically "
                "identifiable target-route region for this Edit: "
                + ", ".join(str(item) for item in owners),
            )
        if relative in self.off_target_paths:
            return self._deny(
                tool,
                relative,
                "This source belongs outside the target page route and is protected by the "
                "multi-page Edit scope.",
            )

        if normalized in {"write", "write_file"} and absolute.exists():
            return self._deny(
                tool,
                relative,
                "Existing frontend source cannot be overwritten in minimal-path mode; "
                "use one exact patch against the harness-selected source location.",
            )

        predecessor = self._dependency_predecessor(relative)
        if relative in self.initial_paths or relative in self.touched_paths:
            tier = "local"
            expansion_reason = None
        elif relative in (self.local_paths | self.dependency_paths) and predecessor:
            tier = "dependency"
            expansion_reason = "recorded_dependency_edge"
            if self.validation_attempt_revision < self.mutation_revision:
                return self._deny(
                    tool,
                    relative,
                    "A post-mutation validation attempt is required before the harness "
                    f"widens from {predecessor} to its dependency {relative}.",
                )
        elif relative in self.local_paths:
            return self._deny(
                tool,
                relative,
                "This candidate is not yet unlocked. Start from the initial path, then "
                "follow a recorded dependency edge after a validation attempt: "
                + ", ".join(sorted(self.initial_paths)),
            )
        elif not absolute.exists():
            return self._deny(
                tool,
                relative,
                "Minimal-path edit/repair does not admit an unplanned new source file. "
                "Patch the selected existing source or follow a recorded dependency edge.",
            )
        else:
            candidates = sorted(self.local_paths | self.dependency_paths)
            return self._deny(
                tool,
                relative,
                "Source path is outside the harness change cone. Start with: "
                + (
                    ", ".join(candidates)
                    if candidates
                    else "no mechanically supported path"
                ),
            )

        if absolute.exists() and relative not in self.observed_paths:
            return self._deny(
                tool,
                relative,
                "Inspect the exact harness-selected source file before mutating it: "
                + relative,
            )

        if (
            relative not in self.touched_paths
            and len(self.touched_paths) >= self.max_touched_files
        ):
            return self._deny(
                tool,
                relative,
                f"Minimal-path touched-file budget is {self.max_touched_files}; "
                "finish within the already authorized files.",
            )

        pairs = self._patch_pairs(tool, tool_input)
        patch_lines = sum(
            max(len(old.splitlines()) or 1, len(new.splitlines()) or 1)
            for old, new in pairs
        )
        if pairs and patch_lines > self.max_patch_lines:
            return self._deny(
                tool,
                relative,
                f"Patch exceeds the {self.max_patch_lines}-line patch-line budget; "
                "split it at the target source boundary.",
                patch_lines=patch_lines,
            )
        if pairs and absolute.is_file():
            content = absolute.read_text(encoding="utf-8", errors="replace")
            for old, new in pairs:
                if not old or content.count(old) != 1:
                    return self._deny(
                        tool,
                        relative,
                        "Minimal-path exact patch must identify one unique source occurrence.",
                        patch_lines=patch_lines,
                    )
                if guarded_shared:
                    start = content.index(old)
                    end = start + len(old)
                    contract = self._guarded_contract_for_span(
                        relative, content, start, end
                    )
                    if contract is None:
                        return self._deny(
                            tool,
                            relative,
                            "Shared source patches must stay wholly inside one harness-owned "
                            "guarded target-route region.",
                            patch_lines=patch_lines,
                        )
                    if contract.get("mutation_mode") == "additive_target_members":
                        additive_error = _validate_additive_target_members(
                            old, new, contract.get("allowed_terms") or []
                        )
                        if additive_error:
                            return self._deny(
                                tool,
                                relative,
                                additive_error,
                                patch_lines=patch_lines,
                            )

        self._record(
            decision="allow",
            tool=tool,
            path=relative,
            reason="mutation stays inside the smallest mechanically supported cone",
            scope_tier="guarded_shared_region" if guarded_shared else tier,
            patch_lines=patch_lines,
            expansion_reason=expansion_reason,
        )
        return None
