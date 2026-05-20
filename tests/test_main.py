from __future__ import annotations

import pytest

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


# --- frontend port configurable via env + CLI ---


def test_build_config_default_frontend_port():
    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)
    assert config.frontend_port == 5173


def test_build_config_frontend_port_from_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_PORT", "4321")
    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)
    assert config.frontend_port == 4321


def test_build_config_frontend_port_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_PORT", "4321")
    args = build_parser().parse_args(
        ["build a counter app", "--frontend-port", "9999"]
    )
    config = build_config(args)
    assert config.frontend_port == 9999


def test_build_config_max_rounds_from_env(monkeypatch):
    monkeypatch.setenv("MAX_ROUNDS", "6")

    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)

    assert config.max_rounds == 6


def test_build_config_max_rounds_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("MAX_ROUNDS", "6")

    args = build_parser().parse_args(
        ["build a counter app", "--max-rounds", "9"]
    )
    config = build_config(args)

    assert config.max_rounds == 9


def test_build_config_max_budget_from_env(monkeypatch):
    monkeypatch.setenv("MAX_BUDGET_USD", "12.5")

    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)

    assert config.max_budget_usd == 12.5


def test_build_config_max_budget_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("MAX_BUDGET_USD", "12.5")

    args = build_parser().parse_args(
        ["build a counter app", "--max-budget", "18.0"]
    )
    config = build_config(args)

    assert config.max_budget_usd == 18.0


def test_build_config_playwright_headless_from_env(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")

    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)

    assert config.playwright_headless is True


def test_build_config_playwright_headless_cli_can_disable_env(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")

    args = build_parser().parse_args(
        ["build a counter app", "--no-playwright-headless"]
    )
    config = build_config(args)

    assert config.playwright_headless is False


def test_build_config_playwright_headless_cli_enables_flag(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "false")

    args = build_parser().parse_args(
        ["build a counter app", "--playwright-headless"]
    )
    config = build_config(args)

    assert config.playwright_headless is True


def test_build_config_design_mode_from_env(monkeypatch):
    monkeypatch.setenv("DESIGN_MODE", "image-first")

    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)

    assert config.design_mode == "image-first"


def test_build_config_design_mode_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("DESIGN_MODE", "text-only")

    args = build_parser().parse_args(
        ["build a counter app", "--design-mode", "image-first"]
    )
    config = build_config(args)

    assert config.design_mode == "image-first"


# --- --plan-only and --resume must be mutually exclusive ---


def test_cli_rejects_plan_only_with_resume():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["build a counter app", "--plan-only", "--resume"]
        )


# --- expose evaluator_vision_model on the CLI ---


def test_build_config_evaluator_vision_model_default_uses_evaluator_model(monkeypatch):
    monkeypatch.delenv("EVALUATOR_VISION_MODEL", raising=False)
    monkeypatch.delenv("EVALUATOR_MODEL", raising=False)
    args = build_parser().parse_args(["build a counter app"])
    config = build_config(args)
    # Without env override the vision model falls back to evaluator_model
    # (which itself defaults to claude-sonnet-4-6).
    assert config.evaluator_vision_model == config.evaluator_model


def test_build_config_evaluator_vision_model_cli_override(monkeypatch):
    args = build_parser().parse_args(
        ["build a counter app", "--evaluator-vision-model", "gpt-4o-mini"]
    )
    config = build_config(args)
    assert config.evaluator_vision_model == "gpt-4o-mini"
