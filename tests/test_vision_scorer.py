from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents import vision_scorer
from src.agents.vision_scorer import (
    _build_review_context,
    _build_vision_messages,
    _provider_model_string,
    _read_image_as_base64,
    _validate_screenshot_path,
    normalize_visual_review,
)
from src.config import HarnessConfig
from src.utils.llm_client import CompletionResult
from src.utils.llm_json import LLMJSONError


# --- _provider_model_string ---


def test_provider_model_string_routes_anthropic_by_default():
    config = HarnessConfig(
        evaluator_vision_model="claude-sonnet-4-6",
        evaluator_vision_endpoint_type="anthropic",
    )
    assert _provider_model_string(config) == "anthropic/claude-sonnet-4-6"


def test_provider_model_string_routes_openai_when_endpoint_is_openai():
    config = HarnessConfig(
        evaluator_vision_model="gpt-4o-mini",
        evaluator_vision_endpoint_type="OpenAI",
    )
    assert _provider_model_string(config) == "openai/gpt-4o-mini"


def test_provider_model_string_blank_endpoint_falls_back_to_anthropic():
    config = HarnessConfig(
        evaluator_vision_model="claude-sonnet-4-6",
        evaluator_vision_endpoint_type="",
    )
    assert _provider_model_string(config) == "anthropic/claude-sonnet-4-6"


def test_provider_model_string_unknown_endpoint_raises():
    config = HarnessConfig(
        evaluator_vision_model="claude-sonnet-4-6",
        evaluator_vision_endpoint_type="azure",
    )
    with pytest.raises(ValueError, match="unsupported evaluator vision endpoint type"):
        _provider_model_string(config)


# --- _build_vision_messages ---


def _make_screenshot(tmp_path: Path) -> Path:
    image = tmp_path / ".harness" / "round_1_home.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return image


def test_build_vision_messages_uses_unified_image_url_block(tmp_path: Path):
    _make_screenshot(tmp_path)
    messages = _build_vision_messages(
        workdir=tmp_path,
        screenshot_paths=[".harness/round_1_home.png"],
        review_context="Review this screenshot.",
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"]
    assert messages[1]["role"] == "user"

    content = messages[1]["content"]
    assert content[0] == {"type": "text", "text": "Review this screenshot."}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_vision_messages_rejects_path_traversal(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    with pytest.raises(ValueError, match="escapes workdir|outside"):
        _build_vision_messages(
            workdir=tmp_path,
            screenshot_paths=["../../../etc/passwd"],
            review_context="x",
        )


def test_build_vision_messages_rejects_non_png_extension(tmp_path: Path):
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir()
    (harness_dir / "credentials").write_bytes(b"data")
    with pytest.raises(ValueError, match="\\.png"):
        _build_vision_messages(
            workdir=tmp_path,
            screenshot_paths=[".harness/credentials"],
            review_context="x",
        )


def test_build_vision_messages_rejects_path_outside_harness_dir(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "logo.png").write_bytes(b"png")
    with pytest.raises(ValueError, match="\\.harness"):
        _build_vision_messages(
            workdir=tmp_path,
            screenshot_paths=["frontend/logo.png"],
            review_context="x",
        )


# --- _validate_screenshot_path ---


def test_validate_screenshot_path_rejects_absolute_path(tmp_path: Path):
    with pytest.raises(ValueError, match="relative to workdir"):
        _validate_screenshot_path("/etc/passwd", tmp_path)


def test_validate_screenshot_path_accepts_valid_png(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "shot.png").write_bytes(b"png")
    resolved = _validate_screenshot_path(".harness/shot.png", tmp_path)
    assert resolved == (tmp_path / ".harness" / "shot.png").resolve()


# --- _read_image_as_base64 ---


def test_read_image_as_base64_returns_ascii_string(tmp_path: Path):
    image = tmp_path / "x.png"
    image.write_bytes(b"\x89PNG")
    encoded = _read_image_as_base64(image)
    assert isinstance(encoded, str)
    # Decoding produces the original bytes
    import base64 as _b64

    assert _b64.b64decode(encoded) == b"\x89PNG"


# --- _build_review_context ---


def _seed_file_comm(tmp_path: Path):
    from src.orchestration.file_comm import FileComm

    harness = tmp_path / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    file_comm = FileComm(harness)
    file_comm.write_spec("# Spec\nDetails.")
    file_comm.write_design_tokens(
        {
            "theme_name": "x",
            "color": {"bg": "#000"},
            "typography": {"display": "Sans"},
            "spacing": {"base": 8},
            "radius": {"card": 12},
            "motion": {"fast": 100},
            "style_rules": ["bold"],
            "anti_patterns": [],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    return file_comm


def test_build_review_context_packs_sprint_and_design_tokens(tmp_path: Path):
    file_comm = _seed_file_comm(tmp_path)

    payload = _build_review_context(
        file_comm=file_comm,
        sprint_num=2,
        sprint_context={
            "title": "Landing",
            "goal": "Build hero",
            "deliverables": ["hero"],
            "exit_criteria": ["renders"],
        },
        screenshot_names=["round_2_home.png"],
    )

    import json as _json

    parsed = _json.loads(payload)
    assert parsed["sprint"] == 2
    assert parsed["sprint_title"] == "Landing"
    assert parsed["sprint_goal"] == "Build hero"
    assert parsed["deliverables"] == ["hero"]
    assert parsed["exit_criteria"] == ["renders"]
    assert parsed["screenshots"] == ["round_2_home.png"]
    assert parsed["design_tokens"]["theme_name"] == "x"
    assert "# Spec" in parsed["spec_excerpt"]


def test_build_review_context_includes_design_contract_when_present(tmp_path: Path):
    file_comm = _seed_file_comm(tmp_path)
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": "image_backed_ui",
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {"design_hypothesis": "Use asymmetry."},
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
        }
    )
    file_comm.write_layout_contract(
        {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        }
    )
    file_comm.write_asset_manifest(
        {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }
    )

    payload = _build_review_context(
        file_comm=file_comm,
        sprint_num=1,
        sprint_context={"title": "Landing"},
        screenshot_names=["round_1_home.png"],
    )

    import json as _json

    parsed = _json.loads(payload)
    assert parsed["design_contract"]["visual_strategy"] == "image_backed_ui"
    assert parsed["design_contract"]["layout_contract"]["asset_fit"] == {
        "background_ui": "cover"
    }


# --- normalize_visual_review ---


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


# --- _perform_visual_review_request (with LiteLLM mock) ---


def _vision_config(**overrides) -> HarnessConfig:
    base: dict[str, Any] = dict(
        evaluator_vision_model="claude-sonnet-4-6",
        evaluator_vision_api_key="test-key",
        evaluator_vision_base_url="https://api.anthropic.com",
        evaluator_vision_endpoint_type="anthropic",
        evaluator_vision_max_tokens=600,
        evaluator_vision_max_retries=3,
        evaluator_vision_retry_base_delay_seconds=0.0,
    )
    base.update(overrides)
    return HarnessConfig(**base)


def _success_review_text() -> str:
    import json as _json

    return _json.dumps(
        {
            "phase_result": "pass",
            "appearance_review": {
                "render_stability": 5,
                "content_relevance": 5,
                "layout_harmony": 5,
                "modernness_memorability": 5,
                "token_adherence": 5,
                "notes": "ok",
            },
            "criteria_scores": {
                "design_quality": {"score": 8, "notes": ""},
                "originality": {"score": 7, "notes": ""},
                "craft": {"score": 8, "notes": ""},
            },
        }
    )


def _seed_workdir(tmp_path: Path) -> tuple[Path, list[str], Any]:
    from src.orchestration.file_comm import FileComm

    harness = tmp_path / ".harness"
    harness.mkdir(parents=True)
    image = harness / "round_1_home.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    file_comm = FileComm(harness)
    file_comm.write_spec("# Spec\n")
    file_comm.write_design_tokens(
        {
            "theme_name": "x",
            "color": {"bg": "#000"},
            "typography": {"display": "Sans"},
            "spacing": {"base": 8},
            "radius": {"card": 12},
            "motion": {"fast": 100},
            "style_rules": ["bold"],
            "anti_patterns": [],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    return tmp_path, [".harness/round_1_home.png"], file_comm


def test_perform_visual_review_request_calls_completion_with_anthropic_model_string(
    monkeypatch, tmp_path
):
    workdir, paths, file_comm = _seed_workdir(tmp_path)

    captured: dict[str, Any] = {}

    def fake_completion(*, messages, model, **kwargs):
        captured["messages"] = messages
        captured["model"] = model
        captured["kwargs"] = kwargs
        return CompletionResult(
            text=_success_review_text(),
            usage={"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
            raw=object(),
        )

    monkeypatch.setattr(vision_scorer, "completion", fake_completion)

    review, stats = vision_scorer._perform_visual_review_request(
        config=_vision_config(),
        file_comm=file_comm,
        workdir=workdir,
        sprint_num=1,
        sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
        screenshot_paths=paths,
    )

    assert review["phase_result"] == "pass"
    assert captured["model"] == "anthropic/claude-sonnet-4-6"
    assert captured["kwargs"]["api_key"] == "test-key"
    assert captured["kwargs"]["api_base"] == "https://api.anthropic.com"
    assert captured["kwargs"]["max_tokens"] == 600
    assert captured["kwargs"]["num_retries"] == 3
    assert captured["kwargs"]["timeout"] == 300
    # System role + single user role with text + image blocks
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"
    assert captured["messages"][1]["content"][1]["type"] == "image_url"
    # Stats reflect the LiteLLM usage dict
    assert stats.token_usage == {
        "prompt_tokens": 120,
        "completion_tokens": 60,
        "total_tokens": 180,
    }
    assert stats.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 60,
        "total_tokens": 180,
    }
    assert stats.model_usage == {"model": "claude-sonnet-4-6"}
    assert stats.cost_usd >= 0


def test_perform_visual_review_request_routes_to_openai_when_configured(
    monkeypatch, tmp_path
):
    workdir, paths, file_comm = _seed_workdir(tmp_path)

    captured: dict[str, Any] = {}

    def fake_completion(*, messages, model, **kwargs):
        captured["model"] = model
        return CompletionResult(
            text=_success_review_text(),
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            raw=object(),
        )

    monkeypatch.setattr(vision_scorer, "completion", fake_completion)

    review, _ = vision_scorer._perform_visual_review_request(
        config=_vision_config(
            evaluator_vision_model="gpt-4o-mini",
            evaluator_vision_endpoint_type="openai",
            evaluator_vision_base_url="https://api.openai.com",
        ),
        file_comm=file_comm,
        workdir=workdir,
        sprint_num=1,
        sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
        screenshot_paths=paths,
    )

    assert review["phase_result"] == "pass"
    assert captured["model"] == "openai/gpt-4o-mini"


def test_perform_visual_review_request_raises_on_missing_model(tmp_path):
    workdir, paths, file_comm = _seed_workdir(tmp_path)
    with pytest.raises(ValueError, match="missing evaluator vision model"):
        vision_scorer._perform_visual_review_request(
            config=_vision_config(evaluator_vision_model=""),
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=1,
            sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
            screenshot_paths=paths,
        )


def test_perform_visual_review_request_raises_on_missing_api_key(tmp_path):
    workdir, paths, file_comm = _seed_workdir(tmp_path)
    with pytest.raises(ValueError, match="missing evaluator vision API key"):
        vision_scorer._perform_visual_review_request(
            config=_vision_config(evaluator_vision_api_key=""),
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=1,
            sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
            screenshot_paths=paths,
        )


def test_perform_visual_review_request_propagates_json_parse_errors(
    monkeypatch, tmp_path
):
    workdir, paths, file_comm = _seed_workdir(tmp_path)

    def fake_completion(*, messages, model, **kwargs):
        return CompletionResult(
            text="this is not JSON at all",
            usage={},
            raw=object(),
        )

    monkeypatch.setattr(vision_scorer, "completion", fake_completion)

    with pytest.raises(LLMJSONError):
        vision_scorer._perform_visual_review_request(
            config=_vision_config(),
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=1,
            sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
            screenshot_paths=paths,
        )
