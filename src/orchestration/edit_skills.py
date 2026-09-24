"""Load only the current subtask's exact, structured capability type."""
from __future__ import annotations

from pathlib import Path
import shutil
import json
import hashlib
from html.parser import HTMLParser
import posixpath
import re

from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.skill_versions import _read as _read_skill_manifest


SKILLS_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
EDIT_SKILLS = {
    "Shopping Cart": "webcompass-shopping-cart",
    "Data Table": "webcompass-data-table",
    "Rich Text Editor": "webcompass-rich-text-editor",
    "Drag & Drop Interface": "webcompass-drag-drop",
    "Tree View": "webcompass-tree-view",
    "Real-time Dashboard": "webcompass-realtime-dashboard",
    "Infinite Scroll": "webcompass-infinite-scroll",
    "Async Form Validation": "webcompass-async-validation",
    "File Upload with Progress": "webcompass-file-upload",
    "Parallax Scrolling": "webcompass-parallax",
    "Page Transitions": "webcompass-page-transitions",
    "Particle Effects": "webcompass-particles",
    "Skeleton Loading": "webcompass-skeleton-loading",
    "User Authentication": "webcompass-authentication",
    "Multi-step Wizard": "webcompass-wizard",
    "Notification Center": "webcompass-notification-center",
}


def selected_edit_skill(metadata: dict) -> str | None:
    primary = (metadata.get("classification") or {}).get("primary") or {}
    task_type = metadata.get("task_type")
    if primary:
        if primary.get("taxonomy") != "webcompass":
            return None
        if task_type and task_type != primary.get("type"):
            raise ValueError("conflicting subtask skill classifications")
        task_type = primary.get("type")
    return EDIT_SKILLS.get(task_type)


def detect_edit_stack(frontend: Path) -> str:
    package = frontend / "package.json"
    if not package.is_file():
        return "vanilla"
    data = json.loads(package.read_text(encoding="utf-8"))
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    if "react" in deps and "vue" in deps:
        raise ValueError("Mixed React/Vue project needs an explicit edit_skill_stack")
    return "react" if "react" in deps else "vue" if "vue" in deps else "vanilla"


def acceptance_recipe(task_type: str) -> dict:
    name = EDIT_SKILLS.get(task_type)
    if not name:
        return {}
    return json.loads((SKILLS_ROOT / name / "references/acceptance.json").read_text())


def _selected_skill_folder(workdir: Path, name: str) -> Path:
    lock = _read_skill_manifest(workdir / '.harness' / 'skill_version_lock.json')
    version = ((lock.get('skills') or {}).get(name) or {}).get('version')
    if not version:
        manifest = _read_skill_manifest(workdir / '.harness' / 'skill_versions.json')
        version = ((manifest.get('skills') or {}).get(name) or {}).get('current')
    if version:
        folder = workdir / '.harness' / 'skill_versions' / name / version
        if not folder.is_dir():
            raise ValueError(f'missing Skill snapshot: {name}@{version}')
        return folder
    return SKILLS_ROOT / name


def selected_reference_files(workdir: Path) -> list[Path]:
    contract = read_edit_task_contract(workdir) or {}
    metadata = contract.get("chain_metadata") or {}
    name = selected_edit_skill(metadata)
    if name is None:
        return []
    stack = metadata.get("edit_skill_stack") or detect_edit_stack(workdir / "frontend")
    if stack not in {"vanilla", "react", "vue"}:
        raise ValueError(f"Unsupported edit Skill stack: {stack}")
    folder = _selected_skill_folder(workdir, name) / "references"
    return sorted(file for file in folder.iterdir() if
        file.suffix == ".css" or
        (stack == "vanilla" and file.suffix == ".js") or
        (stack != "vanilla" and file.suffix == ".mjs") or
        (stack == "react" and file.name == "Component.jsx") or
        (stack == "vue" and file.name == "Component.vue"))


def staged_reference_revisions(workdir: Path) -> dict[str, str]:
    """Trust only current selected library bytes, never a model-written manifest."""
    output = {}
    for original in selected_reference_files(workdir):
        name = original.parent.parent.name
        relative = f".harness/edit_skill/{name}/references/{original.name}"
        staged = workdir / relative
        if not staged.is_file() or staged.read_bytes() != original.read_bytes():
            raise ValueError(f"Selected reference changed or missing: {relative}")
        output[relative] = hashlib.sha256(original.read_bytes()).hexdigest()
    return output


def reference_signatures(file: Path) -> str:
    """Expose the callable contract without expanding the reference body into the prompt."""
    lines = file.read_text(encoding="utf-8").splitlines()
    signatures: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(", stripped):
            block = [stripped]
            cursor = index + 1
            while cursor < len(lines) and ")" not in block[-1] and len(block) < 12:
                block.append(lines[cursor].strip())
                cursor += 1
            signatures.append(" ".join(part for part in block if part))
        elif re.match(r"^(?:export\s+)?(?:default\s+)?function\s+[A-Za-z_$][\w$]*\s*\(", stripped):
            signatures.append(stripped)
        elif re.match(r"^(?:export\s+)?\{[^}]+\};?$", stripped):
            signatures.append(stripped)
        elif stripped in {"destroy() {", "destroy() {"}:
            signatures.append("lifecycle: destroy()")
    if not signatures:
        signatures.append("reference API is documented in SKILL.md; copy the staged file without rewriting it")
    return "\n".join(dict.fromkeys(signatures))


def render_edit_skill(workdir: Path) -> str:
    contract = read_edit_task_contract(workdir) or {}
    metadata = contract.get("chain_metadata") or {}
    name = selected_edit_skill(metadata)
    if name is None:
        return ""
    folder = _selected_skill_folder(workdir, name)
    instruction = (folder / "SKILL.md").read_text(encoding="utf-8")
    staged = workdir / ".harness" / "edit_skill" / name / "references"
    staged.mkdir(parents=True, exist_ok=True)
    files = selected_reference_files(workdir)
    blocks = []
    for file in files:
        shutil.copyfile(file, staged / file.name)
        relative = (staged / file.name).relative_to(workdir).as_posix()
        blocks.append(
            f"Reference source: {relative}\n"
            f"Permitted copy destination: {reference_destinations(workdir)[relative]}\n"
            "Use copy_from to reuse this immutable implementation; do not reproduce its body in the response.\n"
            f"Callable signatures only:\n```text\n{reference_signatures(file)}\n```\n"
        )
    return (
        f"\n\n## Current subtask Skill: {name}\n"
        "Use only the selected stack files below; other variants in SKILL.md are not loaded. "
        "Copy references into permitted frontend component paths with existing atomic "
        "operations: {op:copy_from, source:<reference source>, path:<frontend destination>, "
        "line_edits:[]}. Harness binds the reference SHA; do not regenerate its contents. "
        "Adapt with the output protocol required by the current generator. When a destination "
        "already exists, it is reused with its local changes intact; its CURRENT source context "
        "is authoritative for patches, not the canonical reference below. Preserve the existing source scope.\n"
        + instruction + "\n" + "\n".join(blocks)
    )


def reference_destinations(workdir: Path) -> dict[str, str]:
    """Only admit new selected component files, never arbitrary pages or config."""
    base = "frontend/src/components" if (workdir / "frontend/src").is_dir() else "frontend/components"
    return {
        f".harness/edit_skill/{file.parent.parent.name}/references/{file.name}":
        f"{base}/edit-skills/{file.parent.parent.name}/{file.name}"
        for file in selected_reference_files(workdir)
    }


def vanilla_reference_wiring(workdir: Path, html_paths: set[str]) -> list[dict[str, str]]:
    """Wire copied classic definitions before host scripts on authorized pages."""
    if not any(file.suffix == ".js" for file in selected_reference_files(workdir)):
        return []
    destinations = [path for path in reference_destinations(workdir).values()
                    if (workdir / path).is_file() and Path(path).suffix in {".js", ".css"}]
    patches = []
    class Resources(HTMLParser):
        def __init__(self):
            super().__init__(); self.scripts = []; self.styles = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "script":
                self.scripts.append((attrs.get("src", ""), self.get_starttag_text()))
            if tag == "link" and attrs.get("rel", "").lower() == "stylesheet":
                self.styles.append((attrs.get("href", ""), self.get_starttag_text()))
    for path in sorted(html_paths):
        target = workdir / path
        if target.suffix.lower() not in {".html", ".htm"} or not target.is_file():
            continue
        text = target.read_text(); parsed = Resources(); parsed.feed(text)
        for extension, resources in ((".js", parsed.scripts), (".css", parsed.styles)):
            existing = {posixpath.normpath(posixpath.join(posixpath.dirname(path), url))
                        if not url.startswith("/") else "frontend" + url for url, _ in resources}
            missing = [dest for dest in destinations if Path(dest).suffix == extension and dest not in existing]
            if not missing:
                continue
            if resources:
                anchor = resources[0][1]
            else:
                match = re.search(r"</head\s*>" if extension == ".css" else r"</body\s*>", text, re.I)
                if not match:
                    continue
                anchor = match.group()
            tags = []
            for dest in missing:
                url = posixpath.relpath(dest, posixpath.dirname(path))
                tags.append(f'<script src="{url}"></script>' if extension == ".js"
                            else f'<link rel="stylesheet" href="{url}">')
            patches.append({"path": path, "old_text": anchor, "new_text": "\n".join(tags) + "\n" + anchor})
    return patches
