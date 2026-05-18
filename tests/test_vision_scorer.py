from __future__ import annotations

import io
import json as _json
from pathlib import Path
from urllib import error

import pytest

from src.agents.vision_scorer import (
    _build_anthropic_request,
    _build_chat_completions_url,
    _build_review_context,
    _build_messages_url,
    _build_openai_request,
    _extract_anthropic_message_text,
    _extract_json_object,
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


def test_build_review_context_includes_design_contract_when_present(tmp_path: Path):
    from src.orchestration.file_comm import FileComm

    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_spec("# Spec\n")
    file_comm.write_design_tokens({"theme_name": "x"})
    file_comm.write_design_brief(
        {
            "visual_strategy": "concept_reference_only",
            "reference_files": {
                "approved_concept": ".harness/design/approved_concept.png",
            },
        }
    )
    file_comm.write_layout_contract({"viewport_targets": ["1440x900"]})
    file_comm.write_asset_manifest({"assets": []})

    context = _build_review_context(
        file_comm=file_comm,
        sprint_num=1,
        sprint_context={"title": "Core", "goal": "Ship core UI."},
        screenshot_names=[".harness/round_1_home.png"],
    )
    payload = _json.loads(context)

    assert payload["design_contract"]["design_brief"]["visual_strategy"] == (
        "concept_reference_only"
    )
    assert payload["design_contract"]["layout_contract"]["viewport_targets"] == ["1440x900"]
    assert payload["design_contract"]["asset_manifest"] == {"assets": []}
    assert "When a design contract is present" in payload["instructions"][1]


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


# --- screenshot path validation ---


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


# --- scrub secrets from upstream error detail ---


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


def test_extract_json_object_strict_object():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_handles_prose_around_object():
    text = 'Here is my analysis:\n\n{"phase_result": "pass"}\n\nLet me know.'
    assert _extract_json_object(text) == {"phase_result": "pass"}


def test_extract_json_object_handles_markdown_fence():
    text = "Sure!\n\n```json\n{\"phase_result\": \"pass\"}\n```\n"
    assert _extract_json_object(text) == {"phase_result": "pass"}


def test_extract_json_object_handles_trailing_comma():
    text = '{"a": 1, "b": 2,}'
    assert _extract_json_object(text) == {"a": 1, "b": 2}


def test_extract_json_object_handles_line_comment():
    text = '{\n  "a": 1, // explanatory comment\n  "b": 2\n}'
    assert _extract_json_object(text) == {"a": 1, "b": 2}


def test_extract_json_object_picks_balanced_block_when_two_objects_present():
    # Schema example then real answer; previous impl would slice the union and choke.
    text = (
        'Schema is `{"score": "number"}`.\n'
        'My answer:\n'
        '{"phase_result": "pass", "criteria_scores": {"design_quality": {"score": 8}}}'
    )
    result = _extract_json_object(text)
    assert result["phase_result"] == "pass"
    assert result["criteria_scores"]["design_quality"]["score"] == 8


def test_extract_json_object_raises_with_snippet_on_total_failure():
    bad = "no braces here at all"
    with pytest.raises(ValueError) as exc_info:
        _extract_json_object(bad)
    assert "raw text:" in str(exc_info.value)
    assert "no braces here" in str(exc_info.value)


def test_extract_json_object_redacts_secrets_in_error_snippet():
    bad = "Here is sk-AbCdEfGhIjKlMnOp and that is all"
    with pytest.raises(ValueError) as exc_info:
        _extract_json_object(bad)
    assert "sk-AbCdEfGhIjKlMnOp" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


# --- transient retry ---


class _FakeUrlopenContext:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _make_http_error(status: int) -> error.HTTPError:
    fp = io.BytesIO(b'{"error":{"message":"transient"}}')
    return error.HTTPError(
        url="http://example/v1/messages",
        code=status,
        msg="Service Unavailable",
        hdrs=None,
        fp=fp,
    )


def _vision_config(**overrides) -> HarnessConfig:
    base = dict(
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


def _seed_workdir(tmp_path: Path) -> tuple[Path, list[str], "FileComm"]:
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
        }
    )
    return tmp_path, [".harness/round_1_home.png"], file_comm


def _success_response_body() -> bytes:
    return _json.dumps(
        {
            "content": [
                {
                    "type": "text",
                    "text": _json.dumps(
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
                    ),
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    ).encode("utf-8")


def test_vision_scorer_retries_on_transient_5xx_then_succeeds(monkeypatch, tmp_path):
    from src.agents import vision_scorer

    workdir, paths, file_comm = _seed_workdir(tmp_path)

    calls: list[int] = []

    def fake_urlopen(req, timeout=90):
        calls.append(timeout)
        if len(calls) <= 2:
            raise _make_http_error(503)
        return _FakeUrlopenContext(_success_response_body())

    sleeps: list[float] = []
    monkeypatch.setattr(vision_scorer.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vision_scorer.time, "sleep", lambda s: sleeps.append(s))

    review, stats = vision_scorer._perform_visual_review_request(
        config=_vision_config(),
        file_comm=file_comm,
        workdir=workdir,
        sprint_num=1,
        sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
        screenshot_paths=paths,
    )

    assert review["phase_result"] == "pass"
    assert len(calls) == 3  # 2 failures + 1 success
    assert len(sleeps) == 2
    assert stats.cost_usd >= 0


def test_vision_scorer_gives_up_after_max_retries_on_persistent_5xx(monkeypatch, tmp_path):
    from src.agents import vision_scorer

    workdir, paths, file_comm = _seed_workdir(tmp_path)

    def fake_urlopen(req, timeout=90):
        raise _make_http_error(503)

    monkeypatch.setattr(vision_scorer.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vision_scorer.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match=r"vision scorer HTTP 503"):
        vision_scorer._perform_visual_review_request(
            config=_vision_config(evaluator_vision_max_retries=2),
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=1,
            sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
            screenshot_paths=paths,
        )


def test_vision_scorer_does_not_retry_on_4xx(monkeypatch, tmp_path):
    from src.agents import vision_scorer

    workdir, paths, file_comm = _seed_workdir(tmp_path)

    calls: list[int] = []

    def fake_urlopen(req, timeout=90):
        calls.append(1)
        raise _make_http_error(401)

    monkeypatch.setattr(vision_scorer.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vision_scorer.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match=r"vision scorer HTTP 401"):
        vision_scorer._perform_visual_review_request(
            config=_vision_config(),
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=1,
            sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
            screenshot_paths=paths,
        )

    assert len(calls) == 1


def test_vision_scorer_retries_on_url_error(monkeypatch, tmp_path):
    from src.agents import vision_scorer

    workdir, paths, file_comm = _seed_workdir(tmp_path)

    calls: list[int] = []

    def fake_urlopen(req, timeout=90):
        calls.append(1)
        if len(calls) == 1:
            raise error.URLError("connection reset")
        return _FakeUrlopenContext(_success_response_body())

    monkeypatch.setattr(vision_scorer.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vision_scorer.time, "sleep", lambda s: None)

    review, _ = vision_scorer._perform_visual_review_request(
        config=_vision_config(),
        file_comm=file_comm,
        workdir=workdir,
        sprint_num=1,
        sprint_context={"title": "t", "goal": "g", "deliverables": [], "exit_criteria": []},
        screenshot_paths=paths,
    )

    assert review["phase_result"] == "pass"
    assert len(calls) == 2
