#!/usr/bin/env python3
"""Read-only audit of source projects referenced by a 0921 Edit JSONL shard."""

import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


IMPORT_RE = re.compile(r"(?:from\s+|import\s*\(|require\s*\()(['\"])([^'\"]+)\1")
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "crypto", "events", "fs", "http",
    "https", "module", "os", "path", "process", "stream", "string_decoder",
    "url", "util", "zlib",
}
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "coverage"}
WEB_EXTS = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".css"}
SAFE_DEP_REPAIRS = {
    "lucide-react", "react-router-dom", "zustand", "pinia", "tailwindcss",
    "prop-types", "date-fns",
}


def package_name(spec: str) -> str | None:
    if spec.startswith((".", "/", "@/", "#", "http:", "https:", "node:")):
        return None
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) > 1 else spec
    return spec.split("/", 1)[0]


def source_files(root: Path):
    try:
        for p in root.rglob("*"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.is_file() and p.suffix.lower() in WEB_EXTS:
                yield p
    except OSError:
        return


def audit(path: str, references: int) -> dict:
    root = Path(path)
    reasons = []
    if not root.exists():
        return {"path": path, "references": references, "reasons": ["missing_path"]}
    files = list(source_files(root))
    if not files:
        reasons.append("no_web_files")
    package_path = root / "package.json"
    package = None
    if package_path.exists():
        try:
            package = json.loads(package_path.read_text(errors="replace"))
            if not isinstance(package, dict):
                raise ValueError("not an object")
        except Exception:
            reasons.append("invalid_package_json")
    deps = set()
    scripts = {}
    if isinstance(package, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps.update((package.get(key) or {}).keys())
        scripts = package.get("scripts") or {}
        missing = Counter()
        for file in files:
            try:
                text = file.read_text(errors="replace")
            except OSError:
                continue
            for _, spec in IMPORT_RE.findall(text):
                name = package_name(spec)
                if name and name not in deps and name not in NODE_BUILTINS:
                    missing[name] += 1
        if missing:
            reasons.append("undeclared_deps:" + ",".join(sorted(missing)))
    has_entry = any(p.name.lower() in {"index.html", "index.htm"} for p in root.rglob("*") if p.is_file())
    has_runner = any(k in scripts for k in ("start", "dev", "serve", "preview"))
    if not has_entry and not has_runner:
        reasons.append("no_web_entry")
    return {"path": path, "references": references, "reasons": reasons}


def main(shard: str, out: str):
    refs = defaultdict(int)
    with gzip.open(shard, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            metadata = row.get("metadata") or {}
            path = metadata.get("source_project") or row.get("source_project")
            if path:
                refs[path] += 1
    results = [audit(path, count) for path, count in sorted(refs.items())]
    flagged = [r for r in results if r["reasons"]]
    reason_counts = Counter(reason.split(":", 1)[0] for r in flagged for reason in r["reasons"])
    repair_candidates = []
    quarantine = []
    for item in flagged:
        packages = set()
        for reason in item["reasons"]:
            if reason.startswith("undeclared_deps:"):
                packages.update(filter(None, reason.split(":", 1)[1].split(",")))
        if packages and packages <= SAFE_DEP_REPAIRS:
            repair_candidates.append({**item, "missing_packages": sorted(packages)})
        else:
            quarantine.append({**item, "missing_packages": sorted(packages), "quarantine_reason": "unknown_or_ambiguous_dependency"})
    payload = {
        "shard": shard,
        "unique_mothers": len(results),
        "referenced_rows": sum(refs.values()),
        "flagged_mothers": len(flagged),
        "reason_counts": dict(reason_counts),
        "flagged": flagged,
        "repair_candidates": repair_candidates,
        "quarantine": quarantine,
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({k: payload[k] for k in ("unique_mothers", "referenced_rows", "flagged_mothers", "reason_counts")}, ensure_ascii=False))
    for item in flagged[:30]:
        print(json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit_0921_mothers.py SHARD OUT")
    main(sys.argv[1], sys.argv[2])
