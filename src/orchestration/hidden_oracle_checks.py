"""Harness-owned browser checks withheld from Planner and Generator prompts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from src.orchestration.ui_action_contracts import validate_ui_action_sequence


HIDDEN_ORACLE_CHECKS_NAME = "hidden_oracle_checks.json"
HIDDEN_ORACLE_CHECKS_VERSION = "hidden-oracle-checks-v1"


def write_hidden_oracle_checks(
    harness_dir: Path,
    checks: Iterable[dict[str, Any]],
    *,
    target_routes: Iterable[str],
) -> Path:
    routes = {str(route) for route in target_routes}
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in checks:
        if not isinstance(raw, dict):
            raise ValueError("hidden oracle check must be an object")
        check_id = str(raw.get("id") or "").strip()
        route = str(raw.get("route") or "/")
        actions = raw.get("actions")
        if not check_id or check_id in ids:
            raise ValueError("hidden oracle check ids must be non-empty and unique")
        if route not in routes:
            raise ValueError(
                f"hidden oracle route {route!r} is outside target routes"
            )
        validate_ui_action_sequence(actions)
        ids.add(check_id)
        metadata = {
            key: raw[key]
            for key in ("origin", "repair_type", "risk_reason")
            if isinstance(raw.get(key), str) and str(raw[key]).strip()
        }
        normalized.append(
            {"id": check_id, "route": route, **metadata, "actions": actions}
        )
    if not normalized:
        raise ValueError("hidden oracle checks must not be empty")
    harness_dir.mkdir(parents=True, exist_ok=True)
    path = harness_dir / HIDDEN_ORACLE_CHECKS_NAME
    path.write_text(
        json.dumps(
            {
                "schema_version": HIDDEN_ORACLE_CHECKS_VERSION,
                "owner": "harness",
                "prompt_visibility": "hidden",
                "checks": normalized,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_hidden_oracle_checks(harness_dir: Path) -> list[dict[str, Any]]:
    path = harness_dir / HIDDEN_ORACLE_CHECKS_NAME
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != HIDDEN_ORACLE_CHECKS_VERSION
        or payload.get("owner") != "harness"
        or payload.get("prompt_visibility") != "hidden"
    ):
        raise ValueError(f"invalid hidden oracle artifact: {path}")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"hidden oracle artifact has no checks: {path}")
    for check in checks:
        if not isinstance(check, dict) or not str(check.get("id") or "").strip():
            raise ValueError(f"invalid hidden oracle check: {path}")
        validate_ui_action_sequence(check.get("actions"))
    return checks


def merge_hidden_oracle_checks(
    harness_dir: Path, visible_checks: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    visible = list(visible_checks)
    hidden = read_hidden_oracle_checks(harness_dir)
    visible_ids = {str(item.get("id") or "") for item in visible}
    hidden_ids = {str(item.get("id") or "") for item in hidden}
    collisions = sorted((visible_ids & hidden_ids) - {""})
    if collisions:
        raise ValueError(
            "hidden oracle ids collide with visible checks: " + ", ".join(collisions)
        )
    return visible + hidden


__all__ = [
    "HIDDEN_ORACLE_CHECKS_NAME",
    "merge_hidden_oracle_checks",
    "read_hidden_oracle_checks",
    "write_hidden_oracle_checks",
]
