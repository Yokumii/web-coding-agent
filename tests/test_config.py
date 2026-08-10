from __future__ import annotations

from src.config import HarnessConfig


def test_harness_config_uses_model_environment_variables(monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL", "planner-env-model")
    monkeypatch.setenv("GENERATOR_MODEL", "generator-env-model")
    monkeypatch.setenv("EVALUATOR_MODEL", "evaluator-env-model")
    monkeypatch.setenv("EVALUATOR_VISION_MODEL", "vision-env-model")
    monkeypatch.setenv("EVALUATOR_VISION_API_KEY", "vision-key")
    monkeypatch.setenv("EVALUATOR_VISION_BASE_URL", "https://vision.example.com")
    monkeypatch.setenv("EVALUATOR_VISION_ENDPOINT_TYPE", "openai")
    monkeypatch.setenv("EVALUATOR_VISION_MAX_TOKENS", "1500")
    monkeypatch.setenv("DESIGN_MODE", "image-first")
    monkeypatch.setenv("DESIGN_IMAGE_API_KEY", "draw-key")
    monkeypatch.setenv("DESIGN_IMAGE_BASE_URL", "https://draw.example.com")
    monkeypatch.setenv("DESIGN_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setenv("DESIGN_IMAGE_SIZE", "1536x1024")
    monkeypatch.setenv("DESIGN_IMAGE_TIMEOUT_SECONDS", "222")
    monkeypatch.setenv("MAX_BUDGET_USD", "42.5")
    monkeypatch.setenv("MAX_ROUNDS", "7")
    monkeypatch.setenv("FRONTEND_PORT", "4321")
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "true")
    monkeypatch.setenv("FINAL_PROJECT_MODE", "true")
    monkeypatch.setenv("PLANNER_SCOPE_MODE", "expansive-data")
    monkeypatch.setenv("MINIMAL_PATH_GUIDANCE_ENABLED", "false")
    monkeypatch.setenv("MINIMAL_PATH_MAX_PATCH_LINES", "73")
    monkeypatch.setenv("MINIMAL_PATH_MAX_TOUCHED_FILES", "4")

    config = HarnessConfig()

    assert config.planner_model == "planner-env-model"
    assert config.generator_model == "generator-env-model"
    assert config.evaluator_model == "evaluator-env-model"
    assert config.evaluator_vision_model == "vision-env-model"
    assert config.evaluator_vision_api_key == "vision-key"
    assert config.evaluator_vision_base_url == "https://vision.example.com"
    assert config.evaluator_vision_endpoint_type == "openai"
    assert config.evaluator_vision_max_tokens == 1500
    assert config.design_mode == "image-first"
    assert config.design_image_api_key == "draw-key"
    assert config.design_image_base_url == "https://draw.example.com"
    assert config.design_image_model == "gpt-image-2"
    assert config.design_image_size == "1536x1024"
    assert config.design_image_timeout_seconds == 222
    assert config.max_budget_usd == 42.5
    assert config.max_rounds == 7
    assert config.frontend_port == 4321
    assert config.playwright_headless is True
    assert config.final_project_mode is True
    assert config.planner_scope_mode == "expansive-data"
    assert config.evaluator_mode == "full"
    assert config.minimal_path_guidance_enabled is False
    assert config.minimal_path_max_patch_lines == 73
    assert config.minimal_path_max_touched_files == 4


def test_harness_config_uses_sdk_buffer_environment_variable(monkeypatch):
    monkeypatch.setenv("SDK_MAX_BUFFER_SIZE", str(12 * 1024 * 1024))

    config = HarnessConfig()

    assert config.sdk_max_buffer_size == 12 * 1024 * 1024


def test_harness_config_playwright_headless_accepts_falsey_env(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_HEADLESS", "off")

    config = HarnessConfig()

    assert config.playwright_headless is False
