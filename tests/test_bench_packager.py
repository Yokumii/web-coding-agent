from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from src.bench.packager import (
    EXCLUDED_DIRS,
    PackageCommands,
    build_chat_json,
    detect_package_manager_commands,
    package_frontend,
)


def _make_frontend(root: Path, lockfile: str | None, has_dev: bool = True) -> Path:
    fe = root / "frontend"
    fe.mkdir()
    scripts = {"dev": "vite"} if has_dev else {"start": "node server.js"}
    (fe / "package.json").write_text(json.dumps({"name": "x", "scripts": scripts}))
    if lockfile:
        (fe / lockfile).write_text("")
    return fe


def test_detect_pnpm(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "pnpm-lock.yaml")
    cmd = detect_package_manager_commands(fe)
    assert cmd == PackageCommands(install="pnpm install", start="pnpm dev")


def test_detect_yarn(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "yarn.lock")
    cmd = detect_package_manager_commands(fe)
    assert cmd == PackageCommands(install="yarn install", start="yarn dev")


def test_detect_npm_default(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "package-lock.json")
    cmd = detect_package_manager_commands(fe)
    assert cmd == PackageCommands(install="npm install", start="npm run dev")


def test_detect_no_lockfile_falls_back_to_npm(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, None)
    cmd = detect_package_manager_commands(fe)
    assert cmd == PackageCommands(install="npm install", start="npm run dev")


def test_detect_falls_back_to_run_start_when_no_dev(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "pnpm-lock.yaml", has_dev=False)
    cmd = detect_package_manager_commands(fe)
    assert cmd == PackageCommands(install="pnpm install", start="pnpm run start")


def test_build_chat_json_contains_boltaction_xml() -> None:
    cmd = PackageCommands(install="pnpm install", start="pnpm dev")
    meta = {"harness_verdict": "completed"}

    payload = build_chat_json(cmd, meta)

    content = payload["messages"][-1]["content"]
    assert '<boltAction type="shell">pnpm install</boltAction>' in content
    assert '<boltAction type="start">pnpm dev</boltAction>' in content
    assert payload["_meta"]["harness_verdict"] == "completed"


def test_build_chat_json_parsable_by_extractor() -> None:
    """Mirror webgen's extract_bolt_actions behaviour."""
    cmd = PackageCommands(install="pnpm install", start="pnpm dev")
    payload = build_chat_json(cmd, {})
    text = payload["messages"][-1]["content"]

    shells = re.findall(r'<boltAction type="shell">(.*?)</boltAction>', text, re.DOTALL)
    starts = re.findall(r'<boltAction type="start">(.*?)</boltAction>', text, re.DOTALL)

    assert shells == ["pnpm install"]
    assert starts == ["pnpm dev"]


def test_package_frontend_writes_zip_and_json(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "pnpm-lock.yaml")
    (fe / "src").mkdir()
    (fe / "src" / "main.ts").write_text("console.log('hi')")
    (fe / "node_modules").mkdir()
    (fe / "node_modules" / "ignored.js").write_text("ignored")
    (fe / ".harness").mkdir()
    (fe / ".harness" / "stuff").write_text("internal")
    (fe / "build.log").write_text("log")

    out_zip = tmp_path / "bench_input" / "000001.zip"
    out_json = tmp_path / "bench_input" / "000001.json"

    package_frontend(fe, out_zip, out_json, meta={"harness_verdict": "completed"})

    assert out_zip.is_file()
    assert out_json.is_file()

    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
    # zip stores paths relative to frontend root
    assert "package.json" in names
    assert "src/main.ts" in names
    # excluded dirs/files
    assert not any(n.startswith("node_modules/") for n in names)
    assert not any(n.startswith(".harness/") for n in names)
    assert "build.log" not in names

    payload = json.loads(out_json.read_text())
    assert "pnpm install" in payload["messages"][-1]["content"]


def test_package_frontend_raises_when_frontend_missing(tmp_path: Path) -> None:
    out_zip = tmp_path / "out.zip"
    out_json = tmp_path / "out.json"
    with pytest.raises(FileNotFoundError):
        package_frontend(tmp_path / "missing", out_zip, out_json, meta={})


def test_package_frontend_raises_when_no_package_json(tmp_path: Path) -> None:
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "src").mkdir()
    out_zip = tmp_path / "out.zip"
    out_json = tmp_path / "out.json"
    with pytest.raises(FileNotFoundError):
        package_frontend(fe, out_zip, out_json, meta={})


def test_excluded_dirs_static_set_includes_expected() -> None:
    expected = {"node_modules", ".git", "dist", ".next", ".cache", "coverage", ".harness"}
    assert expected.issubset(EXCLUDED_DIRS)


def test_package_frontend_excludes_sensitive_dotfiles(tmp_path: Path) -> None:
    fe = _make_frontend(tmp_path, "pnpm-lock.yaml")
    (fe / ".env").write_text("SECRET=abc")
    (fe / ".npmrc").write_text("//registry.example.com/:_authToken=xyz")
    (fe / ".DS_Store").write_bytes(b"\x00\x00")
    # .env.example is a template (no secrets) — should be included
    (fe / ".env.example").write_text("SECRET=<set me>")

    out_zip = tmp_path / "out.zip"
    out_json = tmp_path / "out.json"
    package_frontend(fe, out_zip, out_json, meta={})

    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
    assert ".env" not in names
    assert ".npmrc" not in names
    assert ".DS_Store" not in names
    assert ".env.example" in names
