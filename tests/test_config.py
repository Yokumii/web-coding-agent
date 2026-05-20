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

    config = HarnessConfig()

    assert config.planner_model == "planner-env-model"
    assert config.generator_model == "generator-env-model"
    assert config.evaluator_model == "evaluator-env-model"
    assert config.evaluator_vision_model == "vision-env-model"
    assert config.evaluator_vision_api_key == "vision-key"
    assert config.evaluator_vision_base_url == "https://vision.example.com"
    assert config.evaluator_vision_endpoint_type == "openai"
    assert config.evaluator_vision_max_tokens == 1500


def test_harness_config_uses_sdk_buffer_environment_variable(monkeypatch):
    monkeypatch.setenv("SDK_MAX_BUFFER_SIZE", str(12 * 1024 * 1024))

    config = HarnessConfig()

    assert config.sdk_max_buffer_size == 12 * 1024 * 1024


def test_harness_config_uses_design_mode_environment_variable(monkeypatch):
    monkeypatch.setenv("DESIGN_MODE", "image-first")

    config = HarnessConfig()

    assert config.design_mode == "image-first"


def test_harness_config_uses_design_image_environment_variables(monkeypatch):
    monkeypatch.setenv("DESIGN_IMAGE_API_KEY", "image-key")
    monkeypatch.setenv("DESIGN_IMAGE_BASE_URL", "https://draw.example.com")
    monkeypatch.setenv("DESIGN_IMAGE_MODEL", "gpt-image-2-vip")
    monkeypatch.setenv("DESIGN_IMAGE_SIZE", "2048x2048")
    monkeypatch.setenv("DESIGN_IMAGE_TIMEOUT_SECONDS", "240")

    config = HarnessConfig()

    assert config.design_image_api_key == "image-key"
    assert config.design_image_base_url == "https://draw.example.com"
    assert config.design_image_model == "gpt-image-2-vip"
    assert config.design_image_size == "2048x2048"
    assert config.design_image_timeout_seconds == 240
