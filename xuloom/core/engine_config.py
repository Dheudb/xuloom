"""Explicit, immutable configuration for :class:`xuloom.core.engine.QueryEngine`.

Every setting the engine reads at construction lives here, together with the
single place where environment-variable defaults are applied
(:meth:`EngineConfig.resolved`). Hosts can build a config once, copy it with
``config.replace(...)`` and pass it to several engines::

    from xuloom import EngineConfig, QueryEngine

    config = EngineConfig(model="my-model", model_provider="openai",
                          api_base_url="https://example/v1", api_key="...", cwd="/work")
    async with QueryEngine(config) as engine:
        result = await engine.run("Summarize README.md")

Keyword arguments to ``QueryEngine(...)`` are still accepted and are applied on
top of ``config`` with :meth:`EngineConfig.replace`; unknown names raise
``TypeError``.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from xuloom.core.reasoning import EFFORTS

__all__ = [
    "EngineConfig",
    "resolve_model_provider",
    "resolve_primary_model",
    "resolve_fallback_model",
    "environment_base_url",
    "environment_api_key",
]


def resolve_model_provider(provider: Optional[str] = None, model: Optional[str] = None,
                           environ: Mapping[str, str] = os.environ) -> str:
    """Return the model adapter name (``"openai"``, ``"anthropic"`` or a registered one), or ``""``."""
    resolved = str(provider or environ.get("XULOOM_MODEL_PROVIDER", "") or "").strip().lower()
    if resolved in {"anthropic", "openai"}:
        return resolved
    if resolved:
        from xuloom.models.registry import get_adapter
        if get_adapter(resolved) is not None:
            return resolved
        if provider:
            raise ValueError(f"Unknown model provider {resolved!r}: register its ModelAdapter or enable its plugin")
    model_name = str(model or "").strip().lower()
    if model_name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if environ.get("OPENAI_API_KEY") and not environ.get("ANTHROPIC_API_KEY"):
        return "openai"
    if environ.get("ANTHROPIC_API_KEY") or environ.get("ANTHROPIC_BASE_URL"):
        return "anthropic"
    return ""


def resolve_primary_model(provider: str, model: Optional[str], environ: Mapping[str, str] = os.environ) -> str:
    candidate = str(model or "").strip()
    if candidate:
        return candidate
    if provider == "openai":
        return environ.get("OPENAI_MODEL", "") or environ.get("XULOOM_OPENAI_MODEL", "") or ""
    return environ.get("ANTHROPIC_MODEL", "") or environ.get("XULOOM_ANTHROPIC_MODEL", "") or ""


def resolve_fallback_model(provider: str, fallback_model: Optional[str], primary_model: str,
                           environ: Mapping[str, str] = os.environ) -> str:
    candidate = str(fallback_model or "").strip()
    if candidate:
        return candidate
    if provider == "openai":
        return environ.get("OPENAI_FALLBACK_MODEL", "") or environ.get("XULOOM_OPENAI_FALLBACK_MODEL", "") or primary_model
    return environ.get("ANTHROPIC_FALLBACK_MODEL", "") or primary_model


def environment_base_url(provider: str, environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(provider.upper() + "_BASE_URL", "")


def environment_api_key(provider: str, environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(provider.upper() + "_API_KEY", "")


@dataclass(frozen=True)
class EngineConfig:
    """All construction-time settings of a QueryEngine.

    ``None`` means "not set": :meth:`resolved` fills it from the environment or a
    documented default. A resolved config is what ``engine.config`` exposes.
    """

    # --- Model connection -------------------------------------------------
    model: Optional[str] = None
    """Primary model ID. Env fallback: ``OPENAI_MODEL`` / ``ANTHROPIC_MODEL``."""
    model_provider: Optional[str] = None
    """Model adapter: ``"openai"`` (Chat Completions compatible), ``"anthropic"`` or a registered adapter name."""
    api_base_url: Optional[str] = None
    """API root. Env fallback: ``<PROVIDER>_BASE_URL``."""
    api_key: Optional[str] = field(default=None, repr=False)
    """Credential. Env fallback: ``<PROVIDER>_API_KEY``. Never printed in ``repr``."""
    fallback_model: Optional[str] = None
    """Model retried when the primary model fails; defaults to the primary model."""
    network_mode: Optional[str] = None
    """``"auto"``, ``"direct"``, ``"system"`` or ``None`` for the configured default."""
    model_contract: Optional[Mapping[str, Any]] = None
    """Exact capability contract of a saved connection (reasoning/search/inputs)."""

    # --- Reasoning --------------------------------------------------------
    reasoning_effort: Optional[str] = None
    thinking_config: Optional[Mapping[str, Any]] = None
    mode_thinking_overrides: Optional[Mapping[str, Any]] = None

    # --- Workspace and policy --------------------------------------------
    cwd: Optional[str] = None
    """Workspace directory; defaults to the process working directory."""
    execution_mode: Optional[str] = None
    """``"host"`` (default) or a system isolation backend. Env: ``XULOOM_EXECUTION_MODE``."""
    approval_policy: Any = None
    """A :class:`xuloom.security.permissions.ApprovalPolicy` or ``None`` for "ask"."""
    collaboration_mode: Optional[str] = None
    """``"solo"``, ``"auto"`` (default) or ``"ultra"``. Env: ``XULOOM_COLLABORATION_MODE``."""
    local_tokenizer_path: Optional[str] = None
    """Optional tokenizer for token estimates. Env: ``XULOOM_LOCAL_TOKENIZER_PATH``."""

    # --- Agent identity (set by the runtime for subagents) ---------------
    agent_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    agent_depth: int = 0
    agent_role: str = "main"

    # --- Tools ------------------------------------------------------------
    extra_tools: Tuple[Any, ...] = ()
    """Trusted ``BaseAgenticTool`` instances registered at construction."""
    disabled_tools: FrozenSet[str] = frozenset()
    """Built-in tool names that this engine must not offer to the model."""

    # --- Plugins ----------------------------------------------------------
    plugins: Tuple[Any, ...] = ()
    """Plugin objects, modules, ``register(api)`` functions or installed plugin names (see :mod:`xuloom.plugins`)."""
    load_enabled_plugins: bool = False
    """Also apply ``~/.xuloom/plugins.json``: the user's enabled plugins and disabled bundled ones (the CLI and desktop app set this)."""
    bundled_plugins: bool = True
    """Load the plugins shipped with Xuloom (``xuloom.plugins.BUNDLED_PLUGINS``, e.g. ``media``)."""
    disabled_plugins: FrozenSet[str] = frozenset()
    """Plugin names this engine must not load from the bundled or enabled lists."""

    # --- Observability and cost (see docs/observability.md) ---------------
    trace_exporters: Tuple[Any, ...] = ()
    """Objects with ``export(span)`` receiving run/model/tool spans. Env adds ``XULOOM_TRACE_FILE`` / ``XULOOM_TRACE_OTLP``."""
    model_prices: Optional[Mapping[str, Any]] = None
    """USD per million tokens by model ID or pattern; overrides ``~/.xuloom/pricing.json``."""
    max_cost_usd: Optional[float] = None
    """Per-task spending cap, subagents included. Env: ``XULOOM_MAX_COST_USD``."""
    record_responses: Optional[str] = None
    """Append every live model response of the top-level engine to this JSONL cassette
    (replay it with :class:`xuloom.testing.ReplayModel`). Env: ``XULOOM_RECORD_RESPONSES``."""

    def __post_init__(self):
        if self.reasoning_effort is not None and self.reasoning_effort not in EFFORTS:
            raise ValueError("Unsupported reasoning_effort")
        if not isinstance(self.agent_depth, int) or self.agent_depth < 0:
            raise ValueError("agent_depth must be a non-negative integer")
        object.__setattr__(self, "extra_tools", tuple(self.extra_tools or ()))
        object.__setattr__(self, "disabled_tools", frozenset(self.disabled_tools or ()))
        object.__setattr__(self, "plugins", tuple(self.plugins or ()))
        object.__setattr__(self, "disabled_plugins", frozenset(self.disabled_plugins or ()))
        object.__setattr__(self, "trace_exporters", tuple(self.trace_exporters or ()))
        if self.max_cost_usd is not None:
            value = float(self.max_cost_usd)
            if value != value or value < 0 or value == float("inf"):
                raise ValueError("max_cost_usd must be a non-negative number")
            object.__setattr__(self, "max_cost_usd", value)

    @classmethod
    def field_names(cls) -> FrozenSet[str]:
        return frozenset(item.name for item in dataclasses.fields(cls))

    def replace(self, **changes) -> "EngineConfig":
        """Return a copy with ``changes`` applied; unknown names raise ``TypeError``."""
        unknown = set(changes) - self.field_names()
        if unknown:
            raise TypeError("Unknown EngineConfig option(s): " + ", ".join(sorted(unknown)))
        return dataclasses.replace(self, **changes)

    def resolved(self, environ: Optional[Mapping[str, str]] = None) -> "EngineConfig":
        """Apply environment defaults once, returning a fully specified copy."""
        env = os.environ if environ is None else environ
        provider = resolve_model_provider(self.model_provider, self.model, env)
        model = resolve_primary_model(provider, self.model, env)
        max_cost = self.max_cost_usd
        if max_cost is None and str(env.get("XULOOM_MAX_COST_USD", "") or "").strip():
            try:
                max_cost = float(env["XULOOM_MAX_COST_USD"])
            except ValueError as error:
                raise ValueError("XULOOM_MAX_COST_USD must be a number") from error
        return dataclasses.replace(
            self,
            max_cost_usd=max_cost,
            record_responses=(self.record_responses if self.record_responses is not None
                              else (env.get("XULOOM_RECORD_RESPONSES", "") or None)),
            cwd=os.path.abspath(self.cwd or os.getcwd()),
            execution_mode=self.execution_mode or env.get("XULOOM_EXECUTION_MODE", "host"),
            collaboration_mode=self.collaboration_mode or env.get("XULOOM_COLLABORATION_MODE", "auto"),
            model_provider=provider,
            model=model,
            fallback_model=resolve_fallback_model(provider, self.fallback_model, model, env),
            api_base_url=self.api_base_url if self.api_base_url is not None else environment_base_url(provider, env),
            api_key=self.api_key if self.api_key is not None else environment_api_key(provider, env),
            local_tokenizer_path=(self.local_tokenizer_path or env.get("XULOOM_LOCAL_TOKENIZER_PATH", "")
                                  or env.get("XULOOM_LOCAL_TOKENIZER_MODEL", "")),
        )

    def to_public_dict(self) -> dict:
        """Settings without credentials or live objects, for logs and diagnostics."""
        hidden = {"api_key", "approval_policy", "extra_tools", "plugins", "trace_exporters", "model_prices"}
        data = {item.name: getattr(self, item.name) for item in dataclasses.fields(self) if item.name not in hidden}
        data["disabled_tools"] = sorted(self.disabled_tools)
        data["disabled_plugins"] = sorted(self.disabled_plugins)
        data["extra_tools"] = [getattr(tool, "name", type(tool).__name__) for tool in self.extra_tools]
        data["plugins"] = [item if isinstance(item, str) else str(getattr(item, "NAME", None) or getattr(item, "name", None)
                           or getattr(item, "__name__", type(item).__name__)) for item in self.plugins]
        data["trace_exporters"] = [type(item).__name__ for item in self.trace_exporters]
        data["model_prices"] = sorted(self.model_prices or {})
        data["has_api_key"] = bool(self.api_key)
        return data
