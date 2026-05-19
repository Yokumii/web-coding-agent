"""按本地价格表计算各模型 token 成本。

SDK 自带的 `total_cost_usd` 只基于其内置的 Claude 价格表估算；当底层端点
被代理到其他提供方时，该值可能为空，也可能套用错误单价。本模块统一改为
读取 `config/pricing.json`，让预算控制始终基于本地价格配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from src.utils.logger import get_logger

logger = get_logger(__name__)


_DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "pricing.json"
)

_RATE_KEYS = ("input", "output", "cache_read", "cache_creation")

_PRICING_CACHE: dict[str, Any] | None = None
_CACHE_LOCK = Lock()
_WARNED_MODELS: set[str] = set()


def _validate_rates(rates: Any, *, where: str) -> dict[str, float]:
    if not isinstance(rates, dict):
        raise ValueError(f"pricing entry must be an object ({where})")
    cleaned: dict[str, float] = {}
    for key in _RATE_KEYS:
        if key not in rates:
            continue
        value = rates[key]
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(
                f"pricing rate '{key}' must be a non-negative number ({where})"
            )
        cleaned[key] = float(value)
    if not cleaned:
        raise ValueError(
            f"pricing entry must declare at least one of {_RATE_KEYS} ({where})"
        )
    return cleaned


def _load_pricing_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"pricing config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"pricing config must be a JSON object: {path}")

    models_raw = data.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise ValueError(f"pricing config missing non-empty 'models' object: {path}")

    default_raw = data.get("default")
    if not isinstance(default_raw, dict):
        raise ValueError(f"pricing config missing 'default' object: {path}")

    models = {
        name: _validate_rates(rates, where=f"models[{name!r}] in {path}")
        for name, rates in models_raw.items()
    }
    default = _validate_rates(default_raw, where=f"default in {path}")

    return {"models": models, "default": default}


def _get_pricing(path: Path | None) -> dict[str, Any]:
    """读取价格配置；只有默认路径会进入进程内缓存。"""
    global _PRICING_CACHE
    if path is not None:
        return _load_pricing_config(path)
    with _CACHE_LOCK:
        if _PRICING_CACHE is None:
            _PRICING_CACHE = _load_pricing_config(_DEFAULT_PRICING_PATH)
        return _PRICING_CACHE


def reset_pricing_cache() -> None:
    """清空价格配置缓存与未知模型告警记录。"""
    global _PRICING_CACHE
    with _CACHE_LOCK:
        _PRICING_CACHE = None
        _WARNED_MODELS.clear()


def _select_rates(
    pricing: dict[str, Any], model: str
) -> tuple[dict[str, float], bool]:
    """查找模型对应单价，并标记是否命中显式配置。"""
    models: dict[str, dict[str, float]] = pricing["models"]
    default: dict[str, float] = pricing["default"]
    if not model:
        return default, False
    rates = models.get(model)
    if rates is not None:
        return rates, True
    # SDK 有时会在模型名后拼接日期后缀，这里采用最长前缀匹配。
    candidates = sorted(
        (key for key in models if model.startswith(key)),
        key=len,
        reverse=True,
    )
    if candidates:
        return models[candidates[0]], True
    return default, False


def normalize_token_usage(token_usage: dict[str, int]) -> dict[str, int]:
    """将不同提供方的 token 字段归一到四个标准桶。"""
    buckets = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    if not isinstance(token_usage, dict):
        return buckets
    for raw_key, value in token_usage.items():
        if not isinstance(value, int) or value < 0:
            continue
        key = str(raw_key).lower()
        if key in {"input_tokens", "prompt_tokens"}:
            buckets["input"] += value
        elif key in {"output_tokens", "completion_tokens"}:
            buckets["output"] += value
        elif key == "cache_read_input_tokens":
            buckets["cache_read"] += value
        elif key == "cache_creation_input_tokens":
            buckets["cache_creation"] += value
    return buckets


def estimate_cost_usd(
    model: str,
    token_usage: dict[str, int],
    *,
    pricing_path: Path | None = None,
) -> float:
    """按模型与 token 用量计算美元成本。"""
    pricing = _get_pricing(pricing_path)
    rates, matched = _select_rates(pricing, model)
    if not matched and model:
        if model not in _WARNED_MODELS:
            _WARNED_MODELS.add(model)
            logger.warning(
                f"[bold yellow]Pricing[/] unknown model {model!r}; using default "
                "(highest-priced) row from config/pricing.json. Add an entry "
                "if this is a recurring model."
            )
    counts = normalize_token_usage(token_usage)
    cost = (
        counts["input"] * rates.get("input", 0.0)
        + counts["output"] * rates.get("output", 0.0)
        + counts["cache_read"] * rates.get("cache_read", 0.0)
        + counts["cache_creation"] * rates.get("cache_creation", 0.0)
    ) / 1_000_000.0
    return round(cost, 6)
