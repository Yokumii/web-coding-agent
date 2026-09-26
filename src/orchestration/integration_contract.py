"""Small host-wiring contract for one atomic Edit.

This artifact guides Generate and Repair. It is deliberately not an acceptance
gate and does not add browser or regression checks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.orchestration.edit_skills import (
    reference_signatures,
    selected_edit_skill,
    selected_reference_files,
)
from src.orchestration.edit_task_contract import read_edit_task_contract


SCHEMA_VERSION = "edit-integration-contract-v1"


_WIRING_BY_SKILL: dict[str, list[str]] = {
    "Data Table": [
        "Pass the host's existing row collection to the table; do not create replacement records.",
        "Mount the table in the requested route's existing comparison/table section.",
        "Write edit, selection, bulk, filter, sort and pagination effects through the host state flow.",
        "Connect every visible table control to the mounted table instance.",
    ],
    "User Authentication": [
        "Mount the authentication UI at the requested header/account route or section.",
        "Use one host-owned session/user state and update it through authentication callbacks.",
        "Connect login, registration, logout and requested recovery controls to real handlers.",
        "Do not store plaintext credentials or maintain a second independent session.",
    ],
    "Page Transitions": [
        "Bind the transition API to the existing rendered views on the requested route.",
        "Route navigation, overlay open/close and history changes through the mounted transition instance.",
        "Pass the selected transition mode/duration into the implementation so controls change behavior.",
        "Preserve the underlying host view and existing navigation state.",
    ],
    "Shopping Cart": [
        "Build cart items from the host's existing products or offerings, retaining stable IDs.",
        "Mount the cart in the requested route/header/section and expose its real entry control.",
        "Write quantity, removal and checkout effects through one shared host/cart state.",
        "Do not add an unused import, an unmounted cart, or a second product catalog.",
    ],
    "Infinite Scroll": [
        "Page the host's existing filtered collection rather than inventing additional records.",
        "Append loaded items through the same renderer/state used by the existing list or grid.",
        "Connect loading, retry, end-of-content and requested scroll restoration to real lifecycle events.",
        "Keep existing filters, item actions and detail/back behavior attached to appended items.",
    ],
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _identifiers(source_windows: list[dict[str, Any]], instruction: str) -> list[str]:
    terms = {
        term for term in re.findall(r"[a-z][a-z0-9]+", instruction.lower())
        if len(term) > 3
    }
    candidates: list[tuple[int, str]] = []
    patterns = (
        r"\bconst\s+\[([A-Za-z_$][\w$]*)\s*,\s*set[A-Za-z_$][\w$]*\]\s*=\s*useState",
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:useMemo\b|\[|\{|[A-Z][A-Z0-9_]*\b)",
        r"\bimport\s+\{([^}]+)\}\s+from",
    )
    for window in source_windows:
        path = str(window.get("path") or "")
        content = str(window.get("content") or "")
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                names = match.group(1).split(",") if "import" in pattern else [match.group(1)]
                for raw in names:
                    name = raw.strip().split(" as ")[-1].strip()
                    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                        continue
                    words = set(re.findall(
                        r"[a-z][a-z0-9]+",
                        re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower(),
                    ))
                    score = len(words & terms) * 10 + (
                        3 if name.lower() in {"items", "rows", "records", "products", "session", "user"} else 0
                    )
                    candidates.append((score, f"{name} in {path}"))
    ranked = sorted(set(candidates), key=lambda item: (-item[0], item[1]))
    return [value for _score, value in ranked[:5]]


def _mount_point(plan: dict[str, Any], source_windows: list[dict[str, Any]], instruction: str) -> str:
    route_scope = plan.get("route_scope") or {}
    entries = list(route_scope.get("target_page_entries") or [])
    target_file = entries[0] if entries else next(
        (str(item.get("path")) for item in source_windows if item.get("path")),
        "current target source",
    )
    lowered = instruction.lower()
    section = next(
        (label for label in ("header", "hero", "compare", "catalog", "detail", "dashboard", "form", "grid", "table", "footer") if label in lowered),
        "existing requested",
    )
    return f"{section} section in {target_file}"


def _skill_api(workdir: Path) -> list[str]:
    output: list[str] = []
    for file in selected_reference_files(workdir):
        for line in reference_signatures(file).splitlines():
            if line and line not in output:
                output.append(line)
    return output[:8]


def build_integration_contract(
    *, workdir: Path, harness_dir: Path, round_num: int, plan: dict[str, Any]
) -> dict[str, Any]:
    task = read_edit_task_contract(workdir) or {}
    metadata = task.get("chain_metadata") or {}
    instruction = str(metadata.get("instruction") or "")
    context = _read_json(harness_dir / f"edit_context_round_{round_num}.json")
    windows = [item for item in context.get("source_windows") or [] if isinstance(item, dict)]
    target_routes = list(
        (plan.get("route_scope") or {}).get("target_routes")
        or task.get("requested_target_routes") or ["/"]
    )
    task_type = str(metadata.get("task_type") or "")
    api = _skill_api(workdir)
    generic = [
        f"Invoke the selected Skill entry point ({api[0] if api else 'documented public API'}) from the real host flow.",
        "Feed the Skill from the host's existing data/state and write callbacks back to that same owner.",
        "Connect every newly visible control to working behavior; do not leave imports, files or controls unmounted.",
        "Implement the instruction's primary behavior before optional styling or polish.",
    ]
    accepted = [
        item for item in metadata.get("accepted_state_summary") or []
        if isinstance(item, dict)
    ]
    mount_point = _mount_point(plan, windows, instruction)
    contract = {
        "schema_version": SCHEMA_VERSION,
        "owner": "harness",
        "round": round_num,
        "instruction": instruction,
        "task_type": task_type,
        "target_route": target_routes,
        "target_section": mount_point,
        "host_data_state_sources": _identifiers(windows, instruction),
        "mount_point": mount_point,
        "skill": selected_edit_skill(metadata),
        "skill_entry_api": api,
        "required_wiring": (_WIRING_BY_SKILL.get(task_type) or generic)[:4],
        "accepted_state_summary": accepted,
        "must_preserve": [
            "All accepted capabilities listed in accepted_state_summary.",
            "Existing route entries, host data owners, mounted scripts/components and their entry controls.",
            "Existing business records, IDs, callbacks and unrelated DOM/layout.",
        ],
    }
    path = harness_dir / f"integration_contract_round_{round_num}.json"
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return contract


__all__ = ["SCHEMA_VERSION", "build_integration_contract"]
