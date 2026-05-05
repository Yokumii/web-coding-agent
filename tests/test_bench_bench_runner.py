from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.bench.bench_runner import (
    BenchSubprocessResult,
    map_vlm_env,
    run_eval_appearance,
    run_ui_eval,
)


def test_map_vlm_env_uses_evaluator_vision_when_set() -> None:
    base = {
        "EVALUATOR_VISION_API_KEY": "key1",
        "EVALUATOR_VISION_BASE_URL": "https://v.example",
        "EVALUATOR_VISION_MODEL": "vlm-1",
    }
    out = map_vlm_env(base, overrides={})
    assert out["WEBGEN_VLM_API_KEY"] == "key1"
    assert out["WEBGEN_VLM_BASE_URL"] == "https://v.example"
    assert out["WEBGEN_VLM_MODEL"] == "vlm-1"


def test_map_vlm_env_falls_back_to_anthropic_then_openai() -> None:
    base = {"ANTHROPIC_API_KEY": "ak", "OPENAI_BASE_URL": "https://o.example"}
    out = map_vlm_env(base, overrides={})
    assert out["WEBGEN_VLM_API_KEY"] == "ak"
    assert out["WEBGEN_VLM_BASE_URL"] == "https://o.example"
    # default model
    assert out["WEBGEN_VLM_MODEL"] == "Qwen2.5-VL-32B-Instruct"


def test_map_vlm_env_overrides_take_precedence() -> None:
    base = {"EVALUATOR_VISION_API_KEY": "from-env"}
    overrides = {"api_key": "from-flag", "base_url": "https://flag.example", "model": "gpt-4o"}
    out = map_vlm_env(base, overrides=overrides)
    assert out["WEBGEN_VLM_API_KEY"] == "from-flag"
    assert out["WEBGEN_VLM_BASE_URL"] == "https://flag.example"
    assert out["WEBGEN_VLM_MODEL"] == "gpt-4o"


def test_map_vlm_env_preserves_caller_env() -> None:
    base = {"PATH": "/usr/bin", "HOME": "/u/x", "EVALUATOR_VISION_API_KEY": "k"}
    out = map_vlm_env(base, overrides={})
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/u/x"


def test_run_ui_eval_invokes_correct_script(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_invoke(cmd, *, cwd, env, log_path):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        captured["log_path"] = log_path
        return BenchSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.bench_runner._invoke", fake_invoke)

    webgen_dir = tmp_path / "webgen"
    webgen_dir.mkdir()
    in_dir = tmp_path / "bench_input"
    in_dir.mkdir()
    test_file = tmp_path / "sampled.jsonl"
    test_file.write_text("")
    log_path = tmp_path / "ui_eval.log"

    rc = run_ui_eval(
        webgen_dir=webgen_dir,
        in_dir=in_dir,
        test_file=test_file,
        env={"WEBGEN_VLM_API_KEY": "k", "WEBGEN_VLM_BASE_URL": "u",
             "WEBGEN_VLM_MODEL": "m"},
        log_path=log_path,
    )

    assert rc == 0
    assert captured["cmd"][:4] == ["uv", "run", "python", "-u"]
    assert any("ui_test_bolt/ui_eval_with_answer.py" in p for p in captured["cmd"])
    assert captured["cwd"] == webgen_dir
    assert captured["env"]["WEBGEN_VLM_API_KEY"] == "k"
    assert captured["log_path"] == log_path
    # Args
    assert "--in_dir" in captured["cmd"]
    assert str(in_dir) in captured["cmd"]
    assert "--test_file" in captured["cmd"]
    assert str(test_file) in captured["cmd"]


def test_run_eval_appearance_invokes_correct_script(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_invoke(cmd, *, cwd, env, log_path):
        captured["cmd"] = list(cmd)
        return BenchSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.bench_runner._invoke", fake_invoke)

    rc = run_eval_appearance(
        webgen_dir=tmp_path / "webgen",
        in_dir=tmp_path / "bench_input",
        test_file=tmp_path / "sampled.jsonl",
        env={"WEBGEN_VLM_API_KEY": "k"},
        log_path=tmp_path / "eval.log",
    )

    assert rc == 0
    assert any("grade_appearance_bolt_diy/eval_appearance.py" in p for p in captured["cmd"])
    # eval_appearance uses `-t` (positional in_dir + -t test_file)
    assert "-t" in captured["cmd"]
