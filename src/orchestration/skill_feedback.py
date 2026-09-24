"""Evidence-backed routing for reusable Edit Skill improvements.

This module only records a proposed improvement. A Skill revision is enabled by
the caller after the same real sample passes again; sample-specific answers are
never copied into a Skill.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def classify_skill_feedback(grades: dict[str, Any]) -> dict[str, Any]:
    """Classify an observed issue without turning unverified coverage into a bug."""
    bugs = [str(x) for x in grades.get("bugs_found") or []]
    repairs = [str(x) for x in grades.get("repair_instructions") or []]
    checks = [x for x in grades.get("ui_checks") or [] if isinstance(x, dict)]
    observed = [x for x in checks if x.get("status") == "fail"]
    if not observed and not bugs:
        return {"status": "no_observed_failure", "owner": "none", "evidence": []}
    text = " ".join(bugs + repairs).lower()
    if any(token in text for token in ("overlay", "occlud", "intercept", "shortcut", "lifecycle", "destroy")):
        owner = "harness" if any(token in text for token in ("overlay", "intercept", "click", "key")) else "skill"
    elif any(token in text for token in ("reference", "drop", "format", "link", "handler", "mount", "host", "container")):
        owner = "reference" if any(token in text for token in ("reference", "drop", "format", "link")) else "skill"
    else:
        owner = "project"
    return {
        "status": "proposed",
        "owner": owner,
        "evidence": [
            {"check_id": item.get("check_id"), "task": item.get("task"), "expected": item.get("expected_result"), "notes": item.get("notes")}
            for item in observed
        ],
        "confirmed_root_cause": False,
        "validation": {"required": True, "same_sample": True, "regression_check": True},
        "revision_policy": "keep_current_skill_version_until_validation_passes",
    }


def write_skill_feedback(*, workdir: Path, round_num: int, grades: dict[str, Any]) -> dict[str, Any]:
    payload = {"schema_version": "skill-feedback-v1", "round": round_num, **classify_skill_feedback(grades)}
    path = Path(workdir) / ".harness" / f"skill_feedback_round_{round_num}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


__all__ = ["classify_skill_feedback", "write_skill_feedback"]
