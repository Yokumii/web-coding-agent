from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout


def extract_training_commits(repo: Path) -> list[dict[str, Any]]:
    """Return single-parent feat/fix commits and their reproducible diffs."""
    records: list[dict[str, Any]] = []
    commit_ids = _git(repo, "rev-list", "--reverse", "HEAD").splitlines()
    for commit_id in commit_ids:
        parents = _git(repo, "show", "-s", "--format=%P", commit_id).strip().split()
        if len(parents) != 1:
            continue
        subject = _git(repo, "show", "-s", "--format=%s", commit_id).strip()
        prefix = subject.split("(", 1)[0].split(":", 1)[0].strip().lower()
        if prefix not in {"feat", "fix"}:
            continue
        parent = parents[0]
        diff = _git(repo, "diff", "--binary", parent, commit_id)
        if not diff.strip():
            continue
        records.append({
            "type": prefix,
            "commit": commit_id,
            "parent_commit": parent,
            "subject": subject,
            "diff": diff,
        })
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = extract_training_commits(args.repo.resolve())
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
