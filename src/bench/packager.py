from __future__ import annotations

import fnmatch
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXCLUDED_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "dist", ".next", ".cache",
    "coverage", ".harness",
})
EXCLUDED_FILE_PATTERNS: tuple[str, ...] = ("*.log",)


@dataclass(frozen=True)
class PackageCommands:
    install: str
    start: str


def detect_package_manager_commands(frontend_dir: Path) -> PackageCommands:
    fe = Path(frontend_dir)
    if (fe / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (fe / "yarn.lock").exists():
        pm = "yarn"
    else:
        pm = "npm"

    has_dev = _has_script(fe, "dev")
    if pm == "npm":
        install = "npm install"
        start = "npm run dev" if has_dev else "npm run start"
    else:
        install = f"{pm} install"
        start = f"{pm} dev" if has_dev else f"{pm} run start"
    return PackageCommands(install=install, start=start)


def _has_script(frontend_dir: Path, name: str) -> bool:
    pkg = frontend_dir / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = data.get("scripts") or {}
    return isinstance(scripts, dict) and name in scripts


def build_chat_json(commands: PackageCommands, meta: dict[str, Any]) -> dict[str, Any]:
    content = (
        f'<boltAction type="shell">{commands.install}</boltAction>\n'
        f'<boltAction type="start">{commands.start}</boltAction>'
    )
    return {
        "messages": [{"role": "assistant", "content": content}],
        "_meta": dict(meta),
    }


def package_frontend(
    frontend_dir: Path,
    out_zip: Path,
    out_json: Path,
    *,
    meta: dict[str, Any],
) -> None:
    fe = Path(frontend_dir)
    if not fe.is_dir():
        raise FileNotFoundError(f"frontend directory does not exist: {fe}")
    if not (fe / "package.json").is_file():
        raise FileNotFoundError(f"package.json missing in {fe}")

    out_zip = Path(out_zip)
    out_json = Path(out_json)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    commands = detect_package_manager_commands(fe)
    chat = build_chat_json(commands, meta)
    out_json.write_text(json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8")

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _walk_included(fe):
            arcname = path.relative_to(fe).as_posix()
            zf.write(path, arcname)


def _walk_included(root: Path):
    """Yield files under root, respecting EXCLUDED_DIRS and EXCLUDED_FILE_PATTERNS."""
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if entry.is_dir():
                if entry.name in EXCLUDED_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                if any(fnmatch.fnmatch(entry.name, pat) for pat in EXCLUDED_FILE_PATTERNS):
                    continue
                yield entry
