from pathlib import Path
import subprocess

from scripts.extract_training_commits import extract_training_commits


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def test_extracts_only_feat_and_fix_commits_with_parent_diffs(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    target = tmp_path / "app.txt"
    target.write_text("base\n")
    _git(tmp_path, "add", "app.txt")
    _git(tmp_path, "commit", "-m", "chore: baseline")
    target.write_text("base\nfeature\n")
    _git(tmp_path, "add", "app.txt")
    _git(tmp_path, "commit", "-m", "feat(core): add feature")
    target.write_text("base\nfeature fixed\n")
    _git(tmp_path, "add", "app.txt")
    _git(tmp_path, "commit", "-m", "fix(core): repair feature")

    records = extract_training_commits(tmp_path)

    assert [record["type"] for record in records] == ["feat", "fix"]
    assert all(record["parent_commit"] for record in records)
    assert "+feature" in records[0]["diff"]
    assert "+feature fixed" in records[1]["diff"]
