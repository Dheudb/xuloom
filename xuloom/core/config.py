"""
Persistent configuration for Xuloom.

Stores model/provider settings in ~/.xuloom/config.json so they survive
across terminal sessions. Values are loaded into os.environ on startup and
written back when /model changes them.
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from xuloom.branding import configuration_file

CONFIG_DIR = Path(os.environ.get("XULOOM_CONFIG_DIR", str(configuration_file(Path.home(), "config.json").parent))).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"


def normalize_api_base_url(provider: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.fragment:
        raise ValueError("API URL must be a valid HTTP(S) address without a fragment")
    path = parsed.path.rstrip("/")
    if provider == "openai" and path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

# Keys we persist — maps config key → environment variable name
_PERSISTED_KEYS = {
    "network_mode": "XULOOM_NETWORK_MODE",
    "model_provider": "XULOOM_MODEL_PROVIDER",
    "anthropic_model": "ANTHROPIC_MODEL",
    "openai_model": "OPENAI_MODEL",
    "anthropic_base_url": "ANTHROPIC_BASE_URL",
    "openai_base_url": "OPENAI_BASE_URL",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "max_output_tokens": "XULOOM_MAX_OUTPUT_TOKENS",
    "max_turns": "XULOOM_MAX_TURNS",
}


def load_config(*, decrypt_secrets=True) -> dict:
    """Missing configuration is empty; damaged configuration must never be overwritten as empty."""
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError("无法读取模型配置；原文件已保留，请修复配置或从备份恢复。") from error
    if not isinstance(value, dict):
        raise ValueError("模型配置必须是 JSON 对象；原文件已保留。")
    from xuloom.security.credentials import transform
    return transform(value, CONFIG_FILE) if decrypt_secrets else value


def save_config(cfg: dict):
    """Write config dict to disk."""
    from xuloom.core.storage import atomic_json
    if not isinstance(cfg, dict):
        raise ValueError("Configuration must be an object")
    from xuloom.security.credentials import transform
    atomic_json(CONFIG_FILE, transform(cfg, CONFIG_FILE, protect=True))


def update_config(transform, timeout=10):
    """Reload and mutate under the existing OS lease, across CLI/desktop processes."""
    from xuloom.core.session_lock import SessionLock, SessionBusyError
    lease = SessionLock(CONFIG_FILE.parent, "config-writer")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                lease.acquire()
                break
            except SessionBusyError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("模型配置正被另一个进程保存，本次未修改。") from None
                time.sleep(.02)
        cfg = transform(load_config())
        save_config(cfg)
        return cfg
    finally:
        lease.close()


def load_config_into_env():
    """
    Called once at startup. For each persisted key, if the env var is NOT
    already set by the user's shell, inject the saved value.
    """
    cfg = load_config()
    if "model_connections" in cfg and not cfg["model_connections"]:
        from xuloom.core.model_catalog import apply_catalog_env
        apply_catalog_env(cfg)
        cfg = dict(cfg)
        for provider in ("openai", "anthropic"):
            for suffix in ("model", "models", "base_url", "api_key"):
                cfg.pop(provider + "_" + suffix, None)
    if cfg.get("model_connections"):
        from xuloom.core.model_catalog import active_connection, connections, project_active
        cfg = project_active(dict(cfg), active_connection(cfg), connections(cfg))
    for cfg_key, env_key in _PERSISTED_KEYS.items():
        val = cfg.get(cfg_key, "")
        if val and not os.environ.get(env_key):
            if cfg_key.endswith("_api_key"):
                from xuloom.security.credentials import api_key as checked_key, is_protected
                if is_protected(val) and cfg_key != cfg.get("model_provider", "anthropic") + "_api_key":
                    continue  # An unused provider must not block configuring the active one.
                val = checked_key(val)
            os.environ[env_key] = str(val)


def save_current_env_to_config():
    """
    Snapshot current env vars back into the config file.
    Only writes non-empty values.
    """
    def change(cfg):
        for cfg_key, env_key in _PERSISTED_KEYS.items():
            val = os.environ.get(env_key, "")
            if val:
                cfg[cfg_key] = val
            else:
                cfg.pop(cfg_key, None)
        if cfg.get(cfg.get("model_provider", "anthropic") + "_model"):
            from xuloom.core.model_catalog import sync_legacy_connection
            cfg = sync_legacy_connection(cfg)
        return cfg
    update_config(change)


# ── Model Output Token Limits ──────────────────────────────────────

# Slot-reservation cap: most requests use < 5k output tokens, so the
# main loop caps to DEFAULT_CAPPED to avoid wasting slot capacity.
# On overflow the loop escalates to ESCALATED.
DEFAULT_CAPPED_MAX_TOKENS = 16_000
ESCALATED_MAX_TOKENS = 64_000
COMPACT_MAX_OUTPUT_TOKENS = 20_000


def get_model_max_output_tokens(model: str) -> tuple:
    """Return (default, upper_limit) for the given model.

    Env override: set XULOOM_MODEL_MAX_OUTPUT_TOKENS=<number> to force
    a specific limit (useful for local models with small output windows).

    Only genuinely old models with hard low limits need special-casing.
    Everything else gets a generous 64k default; most APIs silently cap
    if the model can't actually produce that many tokens.
    """
    env_val = os.environ.get("XULOOM_MODEL_MAX_OUTPUT_TOKENS", "")
    if env_val:
        try:
            v = int(env_val)
            if v > 0:
                return v, v
        except ValueError:
            pass
    m = model.lower()
    if any(k in m for k in ("claude-3-opus", "claude-3-haiku", "gpt-3.5")):
        return 4_096, 4_096
    if any(k in m for k in ("claude-3-sonnet", "3-5-sonnet", "3-5-haiku")):
        return 8_192, 8_192
    return 64_000, 128_000


async def safe_llm_call(client, model: str, provider: str, messages: list,
                        max_tokens: int, _retried: bool = False, *, usage_callback=None, purpose="summary", guard=None):
    from xuloom.core.output_limits import active_budgets, reserve, settle
    if guard is not None:
        # The engine's auxiliary gate: may refuse (cost cap, strict replay miss) or answer from a
        # recording, in which case no request is sent at all.
        replayed = guard(model, purpose, messages)
        if replayed is not None:
            text, values = replayed
            if usage_callback:
                usage_callback(purpose, {**dict(values or {}), "model": model, "replayed": True})
            return text
    budgets = active_budgets()
    reserved = reserve(max_tokens, budgets) if budgets else max_tokens
    usage = []
    try:
        result = await _safe_llm_call(client, model, provider, messages, reserved, _retried, usage)
        if usage_callback and usage and isinstance(usage[-1], dict): usage_callback(purpose, {**usage[-1], "model": model})
        after = getattr(guard, "after", None)
        if after is not None:  # recording
            after(model, purpose, messages, result, usage[-1] if usage and isinstance(usage[-1], dict) else {})
        return result
    finally:
        if budgets:
            settle(budgets, reserved, usage[-1]["output_tokens"] if usage and isinstance(usage[-1], dict) else 0)


async def _safe_llm_call(client, model, provider, messages, max_tokens, _retried, usage):
    """LLM call with automatic max_tokens fallback.

    If the API rejects the request because max_tokens is too large
    (common with local models), retries once at half the value.
    """
    try:
        from xuloom.models.registry import client_adapter, get_adapter
        adapter = client_adapter(client) or get_adapter(provider)
        if adapter is not None and not adapter.native:
            text, reported = await adapter.complete_text(client, model=model, messages=messages, max_tokens=max_tokens)
            usage.append({"input_tokens": int(reported.get("input_tokens", 0) or 0),
                          "output_tokens": int(reported.get("output_tokens", 0) or 0)})
            return str(text or "")
        if provider == "openai":
            from xuloom.core.model_specs import official
            if official(provider, model, str(getattr(client, 'base_url', ''))):
                resp = await client.responses.create(model=model, input=messages, max_output_tokens=max_tokens, store=False)
                usage.append({'input_tokens': int(getattr(getattr(resp, 'usage', None), 'input_tokens', 0) or 0),
                              'output_tokens': int(getattr(getattr(resp, 'usage', None), 'output_tokens', 0) or 0)})
                if resp.status != 'completed':
                    raise ValueError('Responses summary did not complete; original history retained')
                return resp.output_text
            resp = await client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens,
            )
            usage.append({"input_tokens": int(getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0)})
            return str(
                getattr(getattr(resp.choices[0], "message", None), "content", "") or ""
            )
        else:
            resp = await client.messages.create(
                model=model, messages=messages, max_tokens=max_tokens,
            )
            usage.append({"input_tokens": int(getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0),
                "output_tokens": int(getattr(getattr(resp, "usage", None), "output_tokens", 0) or 0)})
            for block in resp.content:
                if getattr(block, "type", "") == "text":
                    return getattr(block, "text", "")
            return ""
    except Exception as e:
        err = str(e).lower()
        if not _retried and ("max_tokens" in err or "max_output" in err
                             or "maximum" in err or "too large" in err):
            fallback = max(1, max_tokens // 2)
            return await _safe_llm_call(
                client, model, provider, messages, fallback, True, usage
            )
        raise
