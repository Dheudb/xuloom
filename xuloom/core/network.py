"""Explicit model networking, shared by main agents, workers and compaction."""
import os
import inspect

import httpx

from xuloom.core.config import load_config


def model_network_mode(value=None):
    mode = value if value is not None else os.environ.get("XULOOM_NETWORK_MODE") or load_config().get("network_mode", "direct")
    if mode not in {"direct", "system"}:
        raise ValueError("Unknown model network mode")
    return mode


def model_http_client(client_type=None, *, network_mode=None):
    # httpx also reads the Windows registry when trust_env=True. A system
    # proxy can silently break otherwise reachable local/custom model APIs.
    if client_type is not None:
        # SDK factories supply their own timeout/limits and the HTTP library
        # version they require (recent Anthropic SDKs use httpx2).
        return client_type(trust_env=model_network_mode(network_mode) == "system", follow_redirects=False,
                           event_hooks={"request": [_remove_empty_account_headers]})
    return httpx.AsyncClient(
        trust_env=model_network_mode(network_mode) == "system",
        timeout=httpx.Timeout(600, connect=10),
        follow_redirects=False,
        event_hooks={"request": [_remove_empty_account_headers]},
    )


async def _remove_empty_account_headers(request):
    # Explicit empty strings suppress SDK environment fallback. Older SDKs
    # serialize them as empty headers; don't send invalid account selectors.
    for name in ("OpenAI-Organization", "OpenAI-Project"):
        if name in request.headers and not request.headers[name]:
            del request.headers[name]


def anthropic_credentials(api_key):
    """Pass explicit credentials; do not import ambient profile/auth headers."""
    from anthropic._types import Omit
    headers = {}
    for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        name, separator, _ = line.partition(":")
        if separator and name.strip():
            name = name.strip()
            headers[name] = api_key if name.lower() == "x-api-key" else Omit()
    headers.update({"X-Api-Key": api_key, "Authorization": Omit(),
                    "Content-Type": "application/json", "Accept": "application/json", "anthropic-version": "2023-06-01"})
    return {"api_key": api_key, "auth_token": "", "default_headers": headers}


def openai_credentials(api_key, client_type):
    options = {"api_key": api_key, "organization": ""}
    # Older supported SDKs predate project-scoped credentials and do not read
    # OPENAI_PROJECT_ID. Avoid passing a parameter they don't recognize.
    parameters = inspect.signature(client_type).parameters
    if "project" in parameters:
        options["project"] = ""
    if "admin_api_key" in parameters:
        options["admin_api_key"] = ""
    # Only newer SDKs consume OPENAI_CUSTOM_HEADERS; those SDKs also expose
    # Omit. Older SDKs don't need this adapter and keep their native signature.
    import openai
    omit = getattr(openai, "Omit", None)
    if omit is not None and "default_headers" in parameters:
        headers = {}
        for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines():
            name, separator, _ = line.partition(":")
            if separator and name.strip():
                name = name.strip()
                headers[name] = "Bearer " + api_key if name.lower() == "authorization" else omit()
        headers.update({"Authorization": "Bearer " + api_key, "OpenAI-Organization": omit(), "OpenAI-Project": omit(),
                        "Content-Type": "application/json", "Accept": "application/json"})
        options["default_headers"] = headers
    return options


def create_model_client(*, provider, model, api_base_url, api_key, network_mode):
    """One explicit client factory for generation and context compression."""
    key = api_key or ("not-configured" if not model else "")
    if provider == "openai":
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient
        return AsyncOpenAI(base_url=api_base_url or "https://api.openai.com/v1",
            http_client=model_http_client(DefaultAsyncHttpxClient, network_mode=network_mode),
            **openai_credentials(key, AsyncOpenAI))
    if provider in {"anthropic", ""}:
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
        return AsyncAnthropic(base_url=api_base_url or "https://api.anthropic.com",
            http_client=model_http_client(DefaultAsyncHttpxClient, network_mode=network_mode),
            **anthropic_credentials(key))
    raise ValueError("Unsupported model provider")
