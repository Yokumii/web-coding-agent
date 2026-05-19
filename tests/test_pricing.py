from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.orchestration import pricing
from src.orchestration.pricing import (
    estimate_cost_usd,
    normalize_token_usage,
    reset_pricing_cache,
)


@pytest.fixture(autouse=True)
def _reset_pricing_cache_between_tests():
    reset_pricing_cache()
    yield
    reset_pricing_cache()


def _write_pricing(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_estimate_cost_usd_known_model_uses_its_rates(tmp_path: Path):
    config = tmp_path / "pricing.json"
    _write_pricing(
        config,
        {
            "models": {
                "glm-5.1": {"input": 0.3, "output": 1.1},
            },
            "default": {"input": 15.0, "output": 75.0},
        },
    )
    cost = estimate_cost_usd(
        "glm-5.1",
        {"input_tokens": 1_000_000, "output_tokens": 500_000},
        pricing_path=config,
    )
    # 1M * 0.3 + 0.5M * 1.1 = 0.85 USD
    assert cost == pytest.approx(0.85, abs=1e-6)


def test_estimate_cost_usd_unknown_model_falls_back_to_default(tmp_path: Path, caplog):
    config = tmp_path / "pricing.json"
    _write_pricing(
        config,
        {
            "models": {"glm-5.1": {"input": 0.3, "output": 1.1}},
            "default": {"input": 15.0, "output": 75.0},
        },
    )
    with caplog.at_level("WARNING"):
        cost = estimate_cost_usd(
            "brand-new-model-v9",
            {"input_tokens": 1_000_000},
            pricing_path=config,
        )
    # 1M * 15.0 / 1M = 15.0 — default row is the most expensive so an
    # unknown model fails closed against the harness budget gate.
    assert cost == pytest.approx(15.0, abs=1e-6)


def test_estimate_cost_usd_prefix_match_prefers_longest_match(tmp_path: Path):
    config = tmp_path / "pricing.json"
    _write_pricing(
        config,
        {
            "models": {
                "claude-sonnet-4": {"input": 100.0, "output": 200.0},
                "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
            },
            "default": {"input": 999.0, "output": 999.0},
        },
    )
    cost = estimate_cost_usd(
        "claude-sonnet-4-5-20250929",
        {"input_tokens": 1_000_000},
        pricing_path=config,
    )
    # Must pick the longer prefix (claude-sonnet-4-5), not claude-sonnet-4.
    assert cost == pytest.approx(3.0, abs=1e-6)


def test_normalize_token_usage_maps_openai_and_anthropic_keys():
    counts = normalize_token_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 5,
            "irrelevant_counter": 999,
        }
    )
    assert counts == {
        "input": 100,
        "output": 50,
        "cache_read": 10,
        "cache_creation": 5,
    }


def test_normalize_token_usage_ignores_negative_and_non_int_values():
    counts = normalize_token_usage(
        {
            "input_tokens": -50,
            "output_tokens": "500",
            "prompt_tokens": 1,
        }
    )
    assert counts == {
        "input": 1,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
    }


def test_estimate_cost_usd_counts_cache_buckets(tmp_path: Path):
    config = tmp_path / "pricing.json"
    _write_pricing(
        config,
        {
            "models": {
                "claude-sonnet-4-6": {
                    "input": 3.0,
                    "output": 15.0,
                    "cache_read": 0.3,
                    "cache_creation": 3.75,
                },
            },
            "default": {"input": 15.0, "output": 75.0},
        },
    )
    cost = estimate_cost_usd(
        "claude-sonnet-4-6",
        {
            "input_tokens": 1_000,
            "output_tokens": 2_000,
            "cache_read_input_tokens": 3_000,
            "cache_creation_input_tokens": 4_000,
        },
        pricing_path=config,
    )
    expected = round(
        (1_000 * 3.0 + 2_000 * 15.0 + 3_000 * 0.30 + 4_000 * 3.75)
        / 1_000_000.0,
        6,
    )
    assert cost == expected


def test_estimate_cost_usd_rejects_malformed_pricing(tmp_path: Path):
    config = tmp_path / "pricing.json"
    _write_pricing(config, {"models": {}, "default": {"input": 1.0}})
    with pytest.raises(ValueError):
        estimate_cost_usd("x", {"input_tokens": 100}, pricing_path=config)


def test_estimate_cost_usd_warns_once_per_unknown_model(tmp_path: Path, caplog):
    config = tmp_path / "pricing.json"
    _write_pricing(
        config,
        {
            "models": {"glm-5.1": {"input": 0.3, "output": 1.1}},
            "default": {"input": 15.0, "output": 75.0},
        },
    )
    with caplog.at_level("WARNING", logger=pricing.logger.name):
        estimate_cost_usd("mystery-v1", {"input_tokens": 1_000}, pricing_path=config)
        estimate_cost_usd("mystery-v1", {"input_tokens": 2_000}, pricing_path=config)
    unknown_warnings = [
        record for record in caplog.records
        if "mystery-v1" in record.getMessage()
    ]
    assert len(unknown_warnings) == 1
