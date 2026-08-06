"""Bundle one generated Vite artifact into ArtifactsBenchmark's official JSONL schema."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SOURCE_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".json",
    ".vue", ".qml", ".ets", ".wxml", ".wxss", ".json5", ".md", ".sql",
    ".yaml", ".yml",
}
IGNORED_PARTS = {"node_modules", "dist", ".git", ".harness"}
IGNORED_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}


def bundle_dist(dist: Path) -> str:
    html = (dist / "index.html").read_text()

    def inline_script(match: re.Match[str]) -> str:
        source = match.group(1).lstrip("/")
        return f"<script type=\"module\">\n{(dist / source).read_text()}\n</script>"

    def inline_style(match: re.Match[str]) -> str:
        source = match.group(1).lstrip("/")
        return f"<style>\n{(dist / source).read_text()}\n</style>"

    html = re.sub(
        r'<script\s+type="module"(?:\s+crossorigin)?\s+src="([^"]+)"></script>',
        inline_script,
        html,
    )
    return re.sub(
        r'<link\s+rel="stylesheet"(?:\s+crossorigin)?\s+href="([^"]+)">',
        inline_style,
        html,
    )


def readable_source(frontend: Path) -> str:
    submission = frontend / "submission"
    source_root = submission if submission.is_dir() else frontend
    paths = []
    for path in source_root.rglob("*"):
        if not path.is_file() or path.name in IGNORED_FILES:
            continue
        relative = path.relative_to(frontend)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in SOURCE_EXTENSIONS:
            paths.append(path)
    paths.sort(key=lambda path: path.as_posix())
    return "\n\n".join(
        f"===== {path.relative_to(frontend).as_posix()} =====\n"
        f"{path.read_text(errors='replace')}"
        for path in paths
    )


def official_answer(frontend: Path) -> str:
    dist = frontend / "dist"
    if (dist / "index.html").is_file():
        preview = bundle_dist(dist)
    elif (frontend / "index.html").is_file():
        preview = (frontend / "index.html").read_text()
    else:
        raise FileNotFoundError(f"missing artifact HTML: {frontend}")
    source = readable_source(frontend)
    return f"{source}\n\n===== RENDERABLE PREVIEW =====\n{preview}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--runs", type=Path, help="Batch run root containing index_NNNN/frontend/dist")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.queries.read_text().splitlines() if line.strip()]
    rows = rows[args.offset:]
    if args.limit is not None:
        rows = rows[:args.limit]
    if args.runs:
        if args.index is not None or args.dist is not None:
            parser.error("--runs cannot be combined with --index/--dist")
        selected = []
        for row in rows:
            frontend = args.runs / f"index_{int(row['index']):04d}" / "frontend"
            if not frontend.is_dir():
                continue
            answer = official_answer(frontend)
            selected.append({**row, "answer": answer})
    else:
        if args.index is None or args.dist is None:
            parser.error("provide --runs or both --index and --dist")
        row = next(item for item in rows if int(item["index"]) == args.index)
        selected = [{**row, "answer": official_answer(args.dist.parent)}]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in selected
    ))


if __name__ == "__main__":
    main()
