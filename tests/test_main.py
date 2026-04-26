from __future__ import annotations

from src.main import build_config, build_parser


def test_build_config_uses_environment_defaults_when_cli_models_omitted(monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL", "planner-env")
    monkeypatch.setenv("GENERATOR_MODEL", "generator-env")
    monkeypatch.setenv("EVALUATOR_MODEL", "evaluator-env")

    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)

    assert config.planner_model == "planner-env"
    assert config.generator_model == "generator-env"
    assert config.evaluator_model == "evaluator-env"


def test_build_config_cli_models_override_environment(monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL", "planner-env")
    monkeypatch.setenv("GENERATOR_MODEL", "generator-env")
    monkeypatch.setenv("EVALUATOR_MODEL", "evaluator-env")

    args = build_parser().parse_args([
        "build a counter app",
        "--planner-model",
        "planner-cli",
        "--generator-model",
        "generator-cli",
        "--evaluator-model",
        "evaluator-cli",
    ])
    config = build_config(args)

    assert config.planner_model == "planner-cli"
    assert config.generator_model == "generator-cli"
    assert config.evaluator_model == "evaluator-cli"
