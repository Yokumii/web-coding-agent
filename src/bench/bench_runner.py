from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VLM_MODEL = "Qwen2.5-VL-32B-Instruct"


@dataclass
class BenchSubprocessResult:
    returncode: int
    stderr: str


def map_vlm_env(
    base_env: dict[str, str],
    *,
    overrides: dict[str, str | None],
) -> dict[str, str]:
    """Build env dict with WEBGEN_VLM_* set from harness env vars / overrides."""
    out = dict(base_env)

    api_key = (
        (overrides.get("api_key") if overrides else None)
        or base_env.get("EVALUATOR_VISION_API_KEY")
        or base_env.get("ANTHROPIC_API_KEY")
        or base_env.get("OPENAI_API_KEY")
        or ""
    )
    base_url = (
        (overrides.get("base_url") if overrides else None)
        or base_env.get("EVALUATOR_VISION_BASE_URL")
        or base_env.get("ANTHROPIC_BASE_URL")
        or base_env.get("OPENAI_BASE_URL")
        or ""
    )
    model = (
        (overrides.get("model") if overrides else None)
        or base_env.get("EVALUATOR_VISION_MODEL")
        or base_env.get("EVALUATOR_MODEL")
        or DEFAULT_VLM_MODEL
    )

    if api_key:
        out["WEBGEN_VLM_API_KEY"] = api_key
    if base_url:
        out["WEBGEN_VLM_BASE_URL"] = base_url
    out["WEBGEN_VLM_MODEL"] = model
    return out


def run_ui_eval(
    *,
    webgen_dir: Path,
    in_dir: Path,
    test_file: Path,
    env: dict[str, str],
    log_path: Path,
) -> int:
    cmd = [
        "uv", "run", "python", "-u",
        "src/ui_test_bolt/ui_eval_with_answer.py",
        "--in_dir", str(in_dir),
        "--test_file", str(test_file),
    ]
    if env.get("WEBGEN_VLM_API_KEY"):
        cmd += ["--api_key", env["WEBGEN_VLM_API_KEY"]]
    if env.get("WEBGEN_VLM_MODEL"):
        cmd += ["--api_model", env["WEBGEN_VLM_MODEL"]]
    if env.get("WEBGEN_VLM_BASE_URL"):
        cmd += ["--api_base_url", env["WEBGEN_VLM_BASE_URL"]]

    result = _invoke(cmd, cwd=webgen_dir, env=env, log_path=log_path)
    return result.returncode


def run_eval_appearance(
    *,
    webgen_dir: Path,
    in_dir: Path,
    test_file: Path,
    env: dict[str, str],
    log_path: Path,
) -> int:
    # Note: positional in_dir, -t test_file (mirrors README example).
    cmd = [
        "uv", "run", "python", "-u",
        "src/grade_appearance_bolt_diy/eval_appearance.py",
        str(in_dir),
        "-t", str(test_file),
    ]
    if env.get("WEBGEN_VLM_API_KEY"):
        cmd += ["--api_key", env["WEBGEN_VLM_API_KEY"]]
    if env.get("WEBGEN_VLM_MODEL"):
        cmd += ["--api_model", env["WEBGEN_VLM_MODEL"]]
    if env.get("WEBGEN_VLM_BASE_URL"):
        cmd += ["--api_base_url", env["WEBGEN_VLM_BASE_URL"]]

    result = _invoke(cmd, cwd=webgen_dir, env=env, log_path=log_path)
    return result.returncode


def ensure_uv_synced(webgen_dir: Path) -> None:
    """Run `uv sync` once in webgen-bench dir; idempotent (uv caches)."""
    subprocess.run(
        ["uv", "sync"],
        cwd=str(webgen_dir),
        check=True,
    )


def _invoke(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> BenchSubprocessResult:
    """Run subprocess, tee output to log_path. Replaceable in tests."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env={**os.environ, **env},
            stdout=log,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    stderr = proc.stderr or ""
    if stderr:
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n--- STDERR ---\n")
            log.write(stderr)
    return BenchSubprocessResult(returncode=proc.returncode, stderr=stderr)
