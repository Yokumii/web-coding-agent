"""Versioned, evidence-gated lifecycle for reusable Edit Skills."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> Path:
    return root / ".harness" / "skill_versions.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def pin_task_skill(*, workdir: Path, skill: str, version: str, source_sha256: str) -> dict[str, Any]:
    """Pin a task to an immutable version; existing pins are never changed."""
    harness = Path(workdir) / ".harness"
    lock_path = harness / "skill_version_lock.json"
    lock = _read(lock_path)
    current = lock.get("skills") or {}
    if skill in current and current[skill].get("version") != version:
        raise ValueError(f"task already pinned to {skill}@{current[skill].get('version')}")
    current[skill] = {"version": version, "source_sha256": source_sha256}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"schema_version": "skill-lock-v1", "skills": current}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return current[skill]


def propose_skill_version(*, workdir: Path, skill: str, source_dir: Path, evidence_ref: str) -> dict[str, Any]:
    """Copy a candidate into an isolated version directory without enabling it."""
    root = Path(workdir) / ".harness" / "skill_versions" / skill
    manifest_path = _manifest(Path(workdir))
    manifest = _read(manifest_path)
    versions = manifest.setdefault("skills", {}).setdefault(skill, {"current": None, "versions": []})["versions"]
    version = f"v{len(versions) + 1}"
    destination = root / version
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(source_dir, destination)
    files = {p.relative_to(destination).as_posix(): _sha(p) for p in destination.rglob("*") if p.is_file()}
    record = {"version": version, "status": "candidate", "created_at": _now(), "evidence_ref": evidence_ref, "files": files}
    versions.append(record)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def record_validation(*, workdir: Path, skill: str, version: str, sample_id: str, passed: bool, regression_passed: bool, evidence_ref: str) -> dict[str, Any]:
    path = _manifest(Path(workdir)); manifest = _read(path)
    entry = next((x for x in manifest.get("skills", {}).get(skill, {}).get("versions", []) if x.get("version") == version), None)
    if entry is None:
        raise ValueError(f"unknown Skill version: {skill}@{version}")
    entry["validation"] = {"sample_id": sample_id, "passed": bool(passed), "regression_passed": bool(regression_passed), "evidence_ref": evidence_ref, "validated_at": _now()}
    entry["status"] = "validated" if passed and regression_passed else "rejected"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entry


def enable_skill_version(*, workdir: Path, skill: str, version: str) -> dict[str, Any]:
    path = _manifest(Path(workdir)); manifest = _read(path)
    skill_data = manifest.get("skills", {}).get(skill, {})
    entry = next((x for x in skill_data.get("versions", []) if x.get("version") == version), None)
    if entry is None or entry.get("status") != "validated":
        raise ValueError("Skill version must pass sample and regression validation before enable")
    previous = skill_data.get("current")
    skill_data["current"] = version
    entry["status"] = "enabled"
    entry["previous"] = previous
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"skill": skill, "version": version, "previous": previous}


def rollback_skill_version(*, workdir: Path, skill: str, version: str | None = None) -> dict[str, Any]:
    path = _manifest(Path(workdir)); manifest = _read(path)
    skill_data = manifest.get("skills", {}).get(skill, {})
    target = version or next((x.get("previous") for x in reversed(skill_data.get("versions", [])) if x.get("previous")), None)
    if not target:
        raise ValueError(f"no rollback version for {skill}")
    if not any(x.get("version") == target for x in skill_data.get("versions", [])):
        raise ValueError(f"unknown rollback version: {skill}@{target}")
    skill_data["current"] = target
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"skill": skill, "version": target}


__all__ = ["pin_task_skill", "propose_skill_version", "record_validation", "enable_skill_version", "rollback_skill_version"]
