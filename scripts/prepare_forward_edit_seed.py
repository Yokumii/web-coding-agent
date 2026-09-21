#!/usr/bin/env python3
"""Create a fresh, immutable accepted baseline for one forward edit case."""
from __future__ import annotations

import argparse
from collections import Counter
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
from pathlib import Path


_EXTERNAL_URL = re.compile(r"https?://[^\s\"'<>)}]+")
_SCAN_SUFFIXES = {".html", ".css", ".json", ".js", ".ts", ".jsx", ".tsx"}


class _SourceUiHintParser(HTMLParser):
    """Extract navigation/state-entry hints without exposing source code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.navigation: list[dict[str, str]] = []
        self.surfaces: list[dict[str, str]] = []
        self.controls: list[dict[str, object]] = []
        self.outputs: list[dict[str, str]] = []
        self.addressable_items: list[dict[str, str]] = []
        self._anchor: dict[str, object] | None = None
        self._select: dict[str, object] | None = None
        self._option: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        data_name = " ".join(values.get("data-name", "").split())[:120]
        stable_classes = [
            item for item in values.get("class", "").split()
            if re.fullmatch(r"[A-Za-z_][\w-]*", item)
        ]
        if data_name and stable_classes and len(self.addressable_items) < 30:
            escaped = data_name.replace("\\", "\\\\").replace('"', '\\"')
            self.addressable_items.append({
                "tag": tag,
                "data_name": data_name,
                "selector": f'{tag}.{stable_classes[0]}[data-name="{escaped}"]',
            })
        test_id = values.get("data-testid", "")[:100]
        if (
            test_id
            and len(self.outputs) < 24
            and tag not in {"button", "input", "select", "textarea"}
            and re.search(
                r"(?:count|result|status|summary|total|value|volume)",
                test_id,
                re.IGNORECASE,
            )
        ):
            self.outputs.append({
                "tag": tag,
                "selector": f"[data-testid='{test_id}']",
            })
        if tag == "a" and values.get("href") and len(self.navigation) < 24:
            self._anchor = {"href": values["href"][:160], "text": []}
        if tag == "select" and len(self.controls) < 24:
            selectors: list[str] = []
            if values.get("data-testid"):
                selectors.append(f"[data-testid='{values['data-testid'][:100]}']")
            if values.get("id") and re.fullmatch(r"[A-Za-z_][\w-]*", values["id"]):
                selectors.append(f"#{values['id']}")
            if selectors:
                self._select = {
                    "tag": "select",
                    "selector": selectors[0],
                    "selector_aliases": selectors[1:],
                    "options": [],
                }
        elif tag == "option" and self._select is not None:
            self._option = {"value": values.get("value", "")[:120], "text": []}
        if (
            tag in {"main", "nav", "section", "form", "aside"}
            or (values.get("id") and tag in {"div", "ul", "ol"})
        ) and len(self.surfaces) < 24:
            identifier = values.get("id", "")[:100]
            classes = " ".join(values.get("class", "").split()[:4])[:120]
            if identifier or classes:
                self.surfaces.append({
                    "tag": tag,
                    **({"id": identifier} if identifier else {}),
                    **({"class": classes} if classes else {}),
                })

    def handle_data(self, data: str) -> None:
        if self._anchor is not None and str(data).strip():
            self._anchor["text"].append(" ".join(str(data).split()))
        if self._option is not None and str(data).strip():
            self._option["text"].append(" ".join(str(data).split()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            label = " ".join(self._anchor["text"])[:120]
            self.navigation.append({
                "href": str(self._anchor["href"]),
                **({"text": label} if label else {}),
            })
            self._anchor = None
        elif tag == "option" and self._select is not None and self._option is not None:
            label = " ".join(self._option["text"])[:120]
            options = self._select["options"]
            if isinstance(options, list) and len(options) < 30:
                options.append({
                    "value": str(self._option["value"]),
                    **({"text": label} if label else {}),
                })
            self._option = None
        elif tag == "select" and self._select is not None:
            self.controls.append(self._select)
            self._select = None
            self._option = None


class _RuntimeCollectionParser(HTMLParser):
    """Collect repeated rendered classes without retaining runtime HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.classes: Counter[tuple[str, str]] = Counter()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        for class_name in values.get("class", "").split():
            if re.fullmatch(r"[A-Za-z_][\w-]*", class_name):
                self.classes[(tag, class_name)] += 1


def compact_source_ui_contract(
    source_frontend: Path, source_evaluation: Path
) -> dict[str, object]:
    """Build a bounded browser-facing map for a source-blind Edit Planner."""
    pages: list[dict[str, object]] = []
    for html_path in sorted(source_frontend.rglob("*.html"))[:20]:
        if ".git" in html_path.parts:
            continue
        parser = _SourceUiHintParser()
        try:
            parser.feed(html_path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        relative = html_path.relative_to(source_frontend).as_posix()
        route = "/" if relative == "index.html" else "/" + relative
        pages.append({
            "route": route,
            "navigation": parser.navigation,
            "surfaces": parser.surfaces,
            "controls": parser.controls,
            "outputs": parser.outputs,
            "addressable_items": parser.addressable_items,
        })

    observed_controls: list[dict[str, object]] = []
    observed_collections: list[dict[str, object]] = []
    try:
        evaluation = json.loads(source_evaluation.read_text(encoding="utf-8"))
        baseline = evaluation.get("baseline") if isinstance(evaluation, dict) else {}
        for item in (baseline or {}).get("interactive") or []:
            if not isinstance(item, dict) or len(observed_controls) >= 30:
                continue
            control = {
                key: str(item[key])[:120]
                for key in ("selector", "tag", "type", "aria_label", "text")
                if str(item.get(key) or "").strip()
            }
            if control:
                observed_controls.append(control)
        runtime_html = str((baseline or {}).get("html") or "")
        if runtime_html:
            parser = _RuntimeCollectionParser()
            parser.feed(runtime_html)
            observed_collections = [
                {"selector": f".{class_name}", "tag": tag, "count": count}
                for (tag, class_name), count in sorted(
                    parser.classes.items(),
                    key=lambda item: (-item[1], item[0]),
                )
                if count >= 2
            ][:20]
    except (OSError, ValueError, TypeError):
        pass
    known_collection_selectors = {
        str(item.get("selector") or "")
        for item in observed_collections
        if isinstance(item, dict)
    }
    source_selectors: set[str] = set()
    source_control_selectors: set[str] = set()
    for source_path in sorted(source_frontend.rglob("*")):
        if (
            not source_path.is_file()
            or ".git" in source_path.parts
            or source_path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"}
        ):
            continue
        try:
            source = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        source_selectors.update(
            match.group(2)
            for match in re.finditer(
                r"querySelectorAll\(\s*(['\"])(\.[A-Za-z_][\w-]*)\1\s*\)",
                source,
            )
        )
        source_control_selectors.update(
            match.group(2)
            for match in re.finditer(
                r"(?<!All)querySelector\(\s*(['\"])([#.][A-Za-z_][\w-]*)\1\s*\)",
                source,
            )
        )
        source_control_selectors.update(
            "#" + match.group(2)
            for match in re.finditer(
                r"getElementById\(\s*(['\"])([A-Za-z_][\w-]*)\1\s*\)",
                source,
            )
        )
        source_selectors.update(
            "." + match.group(2)
            for match in re.finditer(
                r"\.className\s*=\s*(['\"])([A-Za-z_][\w-]*)\1",
                source,
            )
        )
    observed_collections.extend(
        {"selector": selector, "source": "source-selector"}
        for selector in sorted(source_selectors - known_collection_selectors)
    )
    observed_collections = observed_collections[:20]
    known_control_selectors = {
        str(item.get("selector") or "")
        for item in observed_controls
        if isinstance(item, dict)
    }
    observed_controls.extend(
        {"selector": selector, "source": "source-selector"}
        for selector in sorted(source_control_selectors - known_control_selectors)
    )
    observed_controls = observed_controls[:30]
    return {
        "schema_version": "compact-source-ui-contract-v1",
        "pages": pages,
        "observed_controls": observed_controls,
        "observed_collections": observed_collections,
    }


def external_asset_urls(project_dir: Path) -> list[str]:
    """Inventory remote references for provenance; reverse sources may contain them."""
    values: set[str] = set()
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        for value in _EXTERNAL_URL.findall(path.read_text(errors="ignore")):
            values.add(value.rstrip(",;"))
    return sorted(values)


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, text=True, check=True,
        capture_output=True,
    ).stdout.strip()


def prepare_seed(source_frontend: Path, target_workdir: Path, source_evaluation: Path) -> str:
    """Copy a verified app into a new workdir with exactly one baseline commit."""
    source_frontend = source_frontend.resolve()
    target_workdir = target_workdir.resolve()
    source_evaluation = source_evaluation.resolve()
    if not source_frontend.is_dir():
        raise ValueError(f"source frontend does not exist: {source_frontend}")
    if not source_evaluation.is_file():
        raise ValueError(f"source evaluation does not exist: {source_evaluation}")
    if target_workdir.exists():
        raise ValueError(f"refusing to overwrite existing workdir: {target_workdir}")
    external_urls = external_asset_urls(source_frontend)

    frontend = target_workdir / "frontend"
    shutil.copytree(source_frontend, frontend, ignore=shutil.ignore_patterns(".git"))
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "WebCoding Harness")
    _git(frontend, "config", "user.email", "webcoding-harness@local.invalid")
    _git(frontend, "add", "--all")
    _git(frontend, "commit", "-m", "chore: accepted forward-edit baseline")
    baseline = _git(frontend, "rev-parse", "HEAD")
    payload = {
        "status": "ok",
        "source_frontend": str(source_frontend),
        "source_evaluation": str(source_evaluation),
        "baseline_commit": baseline,
        # Do not reject external assets: the reverse-built WebCompass source
        # projects use the same pattern (e.g. fonts and image CDN URLs).  The
        # list makes this environment dependency explicit and replayable.
        "asset_policy": "match_reverse_source",
        "external_asset_urls": external_urls,
        "source_ui_contract": compact_source_ui_contract(
            source_frontend, source_evaluation
        ),
    }
    (target_workdir / "seed_manifest.json").write_text(json.dumps(payload, indent=2) + "\n")
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-frontend", type=Path, required=True)
    parser.add_argument("--source-evaluation", type=Path, required=True)
    parser.add_argument("--target-workdir", type=Path, required=True)
    args = parser.parse_args()
    baseline = prepare_seed(args.source_frontend, args.target_workdir, args.source_evaluation)
    print(json.dumps({"status": "ok", "baseline_commit": baseline}))


if __name__ == "__main__":
    main()
