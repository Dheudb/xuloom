"""Model prices, request cost and the per-task spending cap.

Prices are USD per million tokens. Xuloom ships no price list (they change and
differ between providers); set them for the models you use, either in
``~/.xuloom/pricing.json``::

    {"models": {
        "claude-sonnet-*": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3},
        "gpt-4.1":         {"input": 2, "output": 8}
    }}

or with ``EngineConfig(model_prices={...})``, which takes precedence. Keys are
exact model IDs or ``fnmatch`` patterns; an exact ID wins over a pattern.

Token counts follow the engine's usage record: ``input_tokens`` excludes cache
reads and writes, which are priced separately (``cache_write`` / ``cache_read``
default to the input price, which over- rather than under-estimates).

``EngineConfig(max_cost_usd=...)`` (env ``XULOOM_MAX_COST_USD``) caps one task,
subagents included: before each model request the engine stops with reason
``cost_limit`` once the task's priced cost reached the cap, or when the model
about to be called has no price. The request in flight may overshoot the cap.
"""
from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = ["ModelPrice", "CostMeter", "CostLimitError", "load_prices", "load_pricing_settings",
           "save_pricing_settings", "pricing_path"]


class CostLimitError(RuntimeError):
    """A model request was refused before it started because of the task cost cap."""


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_write: Optional[float] = None
    cache_read: Optional[float] = None

    @classmethod
    def coerce(cls, value: Any) -> "ModelPrice":
        if isinstance(value, ModelPrice):
            price = value
        elif isinstance(value, Mapping):
            unknown = set(value) - {"input", "output", "cache_write", "cache_read"}
            if unknown:
                raise ValueError("Unknown price field(s): " + ", ".join(sorted(unknown)))
            values = {key: (None if item is None else float(item)) for key, item in value.items()}
            if values.get("input") is None or values.get("output") is None:
                raise ValueError("A model price needs input and output prices")
            price = cls(**values)  # type: ignore[arg-type]
        else:
            raise TypeError("A model price is a ModelPrice or a mapping with input/output prices")
        for name in ("input", "output", "cache_write", "cache_read"):
            item = getattr(price, name)
            if item is not None and (not math.isfinite(item) or item < 0):
                raise ValueError(f"Price {name} must be a non-negative number")
        return price

    def cost(self, usage: Mapping[str, Any]) -> float:
        def tokens(name):
            return max(0, int(usage.get(name, 0) or 0))
        cache_write = self.input if self.cache_write is None else self.cache_write
        cache_read = self.input if self.cache_read is None else self.cache_read
        return (tokens("input_tokens") * self.input + tokens("output_tokens") * self.output
                + tokens("cache_creation_input_tokens") * cache_write
                + tokens("cache_read_input_tokens") * cache_read) / 1_000_000


def pricing_path(home: Optional[Path] = None) -> Path:
    return Path(home or Path.home()) / ".xuloom" / "pricing.json"


def load_prices(path: Optional[Path] = None) -> Dict[str, ModelPrice]:
    """Read ``pricing.json``; a missing file means no prices, a malformed one raises ``ValueError``."""
    return load_pricing_settings(path)[0]


def load_pricing_settings(path: Optional[Path] = None) -> Tuple[Dict[str, ModelPrice], Optional[float]]:
    """``(prices, max_cost_usd)`` from ``pricing.json``; the cap is optional (``"max_cost_usd": 0.5``)."""
    path = Path(path) if path is not None else pricing_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read {path}: {error}") from error
    cap = raw.get("max_cost_usd") if isinstance(raw, dict) else None
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < 0):
        raise ValueError(f"{path}: max_cost_usd must be a non-negative number")
    return _parse_models(path, raw), (None if cap is None else float(cap))


def save_pricing_settings(models: Mapping[str, Any], max_cost_usd: Optional[float], path: Optional[Path] = None) -> Path:
    """Validate and write ``pricing.json`` atomically (used by the desktop settings)."""
    path = Path(path) if path is not None else pricing_path()
    prices = {str(name).strip(): ModelPrice.coerce(value) for name, value in (models or {}).items()}
    if any(not name for name in prices):
        raise ValueError("Model names or patterns cannot be empty")
    if max_cost_usd is not None:
        max_cost_usd = float(max_cost_usd)
        if not math.isfinite(max_cost_usd) or max_cost_usd < 0:
            raise ValueError("max_cost_usd must be a non-negative number")
    payload: Dict[str, Any] = {"models": {name: {key: value for key, value in vars(price).items() if value is not None}
                                          for name, price in prices.items()}}
    if max_cost_usd is not None:
        payload["max_cost_usd"] = max_cost_usd
    from xuloom.core.storage import atomic_json
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, payload)
    return path


def _parse_models(path, raw) -> Dict[str, ModelPrice]:
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        raise ValueError(f"{path} must contain {{\"models\": {{\"<model>\": {{\"input\": ..., \"output\": ...}}}}}}")
    try:
        return {str(name): ModelPrice.coerce(value) for name, value in models.items()}
    except (TypeError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from error


class CostMeter:
    """Prices requests and enforces the task cap. Subagents share their parent's meter."""

    def __init__(self, prices: Optional[Mapping[str, Any]] = None, *, limit_usd: Optional[float] = None):
        self.prices: Dict[str, ModelPrice] = {str(name): ModelPrice.coerce(value) for name, value in (prices or {}).items()}
        if limit_usd is not None and (not math.isfinite(float(limit_usd)) or float(limit_usd) < 0):
            raise ValueError("max_cost_usd must be a non-negative number")
        self.limit_usd = None if limit_usd is None else float(limit_usd)
        self.task_usd = 0.0
        self.task_unpriced = 0

    def price_for(self, model: Optional[str]) -> Optional[ModelPrice]:
        name = str(model or "")
        if not name:
            return None
        if name in self.prices:
            return self.prices[name]
        for pattern, price in self.prices.items():
            if any(char in pattern for char in "*?[") and fnmatch.fnmatchcase(name, pattern):
                return price
        return None

    def start_task(self) -> None:
        self.task_usd = 0.0
        self.task_unpriced = 0

    def record(self, model: Optional[str], usage: Mapping[str, Any]) -> Optional[float]:
        """Add one request to the task total; returns its cost or ``None`` when the model has no price."""
        price = self.price_for(model)
        if price is None:
            self.task_unpriced += 1
            return None
        cost = price.cost(usage)
        self.task_usd += cost
        return cost

    def blocked_reason(self, model: Optional[str]) -> Optional[str]:
        """Why the next request to ``model`` must not start, or ``None``."""
        if self.limit_usd is None:
            return None
        if self.task_usd >= self.limit_usd:
            return (f"Task cost limit reached: ${self.task_usd:.4f} spent of ${self.limit_usd:.4f}. "
                    "Raise max_cost_usd (XULOOM_MAX_COST_USD) or continue in a new task.")
        if self.price_for(model) is None:
            return (f"A task cost limit is set but model {model!r} has no price, so its cost cannot be limited. "
                    "Add it to ~/.xuloom/pricing.json or EngineConfig.model_prices.")
        if self.task_unpriced:
            return ("A task cost limit is set but earlier requests in this task used unpriced models; "
                    "add their prices before continuing.")
        return None
