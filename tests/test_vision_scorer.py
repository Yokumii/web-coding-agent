from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.vision_scorer import (
    _build_anthropic_request,
    _build_chat_completions_url,
    _build_messages_url,
    _build_openai_request,
    _extract_anthropic_message_text,
    _extract_openai_message_text,
    _normalize_endpoint_type,
    _scrub_secrets,
    normalize_visual_review,
)
from src.config import HarnessConfig


def test_build_messages_url_appends_messages_endpoint():
    assert _build_messages_url("") == "https://api.anthropic.com/v1/messages"
    assert _build_messages_url("https://example.com/base") == "https://example.com/base/v1/messages"
    assert _build_messages_url("https://example.com/v1") == "https://example.com/v1/messages"


def test_build_chat_completions_url_appends_endpoint():
    assert _build_chat_completions_url("") == "https://api.openai.com/v1/chat/completions"
    assert (
        _build_chat_completions_url("https://example.com/base")
        == "https://example.com/base/v1/chat/completions"
    )
    assert (
        _build_chat_completions_url("https://example.com/v1")
        == "https://example.com/v1/chat/completions"
    )


def test_normalize_endpoint_type_accepts_known_values():
    assert _normalize_endpoint_type("anthropic") == "anthropic"
    assert _normalize_endpoint_type("OPENAI") == "openai"


def test_build_openai_request_uses_chat_completions_shape(tmp_path: Path):
    image_path = tmp_path / ".harness" / "round_1_home.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"png")
    config = HarnessConfig(
        evaluator_vision_model="gpt-4o-mini",
        evaluator_vision_api_key="test-key",
        evaluator_vision_base_url="https://api.openai.com",
        evaluator_vision_endpoint_type="openai",
        evaluator_vision_max_tokens=600,
    )

    endpoint, headers, payload = _build_openai_request(
        config=config,
        workdir=tmp_path,
        screenshot_paths=[".harness/round_1_home.png"],
        review_context="Review this screenshot.",
    )

    assert endpoint == "https://api.openai.com/v1/chat/completions"
    assert headers["authorization"] == "Bearer test-key"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"][0] == {"type": "text", "text": "Review this screenshot."}
    assert payload["messages"][1]["content"][1]["type"] == "image_url"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_anthropic_request_uses_messages_shape(tmp_path: Path):
    image_path = tmp_path / ".harness" / "round_1_home.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"png")
    config = HarnessConfig(
        evaluator_vision_model="claude-sonnet-4-5",
        evaluator_vision_api_key="test-key",
        evaluator_vision_base_url="https://api.anthropic.com",
        evaluator_vision_endpoint_type="anthropic",
        evaluator_vision_max_tokens=600,
    )

    endpoint, headers, payload = _build_anthropic_request(
        config=config,
        workdir=tmp_path,
        screenshot_paths=[".harness/round_1_home.png"],
        review_context="Review this screenshot.",
    )

    assert endpoint == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["system"]
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "Review this screenshot."}
    assert payload["messages"][0]["content"][1]["type"] == "image"


def test_extract_openai_message_text_supports_string_and_block_formats():
    assert _extract_openai_message_text(
        {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    ) == "{\"ok\": true}"
    assert _extract_openai_message_text(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "{\"ok\": true}"},
                        ]
                    }
                }
            ]
        }
    ) == "{\"ok\": true}"


def test_extract_anthropic_message_text_reads_text_blocks():
    assert _extract_anthropic_message_text(
        {"content": [{"type": "text", "text": "{\"ok\": true}"}]}
    ) == "{\"ok\": true}"


def test_normalize_visual_review_uses_zero_fallback_for_nonnumeric_scores():
    # Non-numeric / missing scores must collapse to 0.0 so that downstream
    # check_grades fails closed instead of silently passing at the threshold.
    normalized = normalize_visual_review(
        {
            "phase_result": "pass",
            "appearance_review": {},
            "criteria_scores": {
                "design_quality": {"score": "n/a", "notes": ""},
                "originality": {"score": None, "notes": ""},
                "craft": {"score": "bad", "notes": ""},
            },
        },
        [".harness/round_1_home.png"],
    )

    assert normalized["criteria_scores"]["design_quality"]["score"] == 0.0
    assert normalized["criteria_scores"]["originality"]["score"] == 0.0
    assert normalized["criteria_scores"]["craft"]["score"] == 0.0


def test_normalize_visual_review_clamps_values_and_preserves_screenshots():
    normalized = normalize_visual_review(
        {
            "phase_result": "unexpected",
            "appearance_review": {
                "render_stability": 8,
                "content_relevance": 0,
                "layout_harmony": 3,
                "modernness_memorability": 4.4,
                "token_adherence": "bad",
                "notes": "Readable overall.",
            },
            "criteria_scores": {
                "design_quality": {"score": 11, "notes": "Strong hierarchy."},
                "originality": {"score": -1, "notes": "Safe choices."},
                "craft": {"score": 6.26, "notes": "Solid spacing."},
            },
        },
        [".harness/round_2_home.png"],
    )

    assert normalized["phase_result"] == "pass"
    assert normalized["appearance_review"]["screenshots"] == [".harness/round_2_home.png"]
    assert normalized["appearance_review"]["render_stability"] == 5
    assert normalized["appearance_review"]["content_relevance"] == 1
    assert normalized["appearance_review"]["modernness_memorability"] == 4
    assert normalized["appearance_review"]["token_adherence"] == 3
    assert normalized["criteria_scores"]["design_quality"]["score"] == 10.0
    assert normalized["criteria_scores"]["originality"]["score"] == 0.0
    assert normalized["criteria_scores"]["craft"]["score"] == 6.3


# --- Batch 5 (H4): screenshot path validation ---


def test_build_anthropic_request_rejects_path_traversal_in_screenshot(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    config = HarnessConfig(
        evaluator_vision_model="claude",
        evaluator_vision_api_key="x",
        evaluator_vision_base_url="https://api.anthropic.com",
        evaluator_vision_endpoint_type="anthropic",
    )
    with pytest.raises(ValueError, match="escapes workdir|outside"):
        _build_anthropic_request(
            config=config,
            workdir=tmp_path,
            screenshot_paths=["../../../etc/passwd"],
            review_context="x",
        )


def test_build_anthropic_request_rejects_non_png_extension(tmp_path: Path):
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir()
    (harness_dir / "credentials").write_bytes(b"data")
    config = HarnessConfig(
        evaluator_vision_model="claude",
        evaluator_vision_api_key="x",
        evaluator_vision_base_url="https://api.anthropic.com",
        evaluator_vision_endpoint_type="anthropic",
    )
    with pytest.raises(ValueError, match="\\.png"):
        _build_anthropic_request(
            config=config,
            workdir=tmp_path,
            screenshot_paths=[".harness/credentials"],
            review_context="x",
        )


def test_build_anthropic_request_rejects_screenshot_outside_harness_dir(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "logo.png").write_bytes(b"png")
    config = HarnessConfig(
        evaluator_vision_model="claude",
        evaluator_vision_api_key="x",
        evaluator_vision_base_url="https://api.anthropic.com",
        evaluator_vision_endpoint_type="anthropic",
    )
    with pytest.raises(ValueError, match="\\.harness"):
        _build_anthropic_request(
            config=config,
            workdir=tmp_path,
            screenshot_paths=["frontend/logo.png"],
            review_context="x",
        )


def test_build_openai_request_also_validates_screenshot_path(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    config = HarnessConfig(
        evaluator_vision_model="gpt",
        evaluator_vision_api_key="x",
        evaluator_vision_base_url="https://api.openai.com",
        evaluator_vision_endpoint_type="openai",
    )
    with pytest.raises(ValueError, match="escapes workdir|outside"):
        _build_openai_request(
            config=config,
            workdir=tmp_path,
            screenshot_paths=["../../etc/hosts"],
            review_context="x",
        )


# --- Batch 5 (M11): scrub secrets from upstream error detail ---


def test_scrub_secrets_redacts_anthropic_api_key():
    detail = "Auth failed for x-api-key: sk-ant-abc1234567890_-XYZ"
    scrubbed = _scrub_secrets(detail)
    assert "sk-ant-abc1234567890_-XYZ" not in scrubbed
    assert "***" in scrubbed


def test_scrub_secrets_redacts_bearer_token():
    detail = "Forbidden — Bearer eyJhbGc.payload.sig"
    scrubbed = _scrub_secrets(detail)
    assert "eyJhbGc.payload.sig" not in scrubbed
    assert "***" in scrubbed


def test_scrub_secrets_truncates_long_input():
    detail = "x" * 4096
    scrubbed = _scrub_secrets(detail, limit=512)
    assert len(scrubbed) <= 512 + len("...[truncated]")
