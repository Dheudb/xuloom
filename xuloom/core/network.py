"""Explicit model networking, shared by main agents, workers and compaction."""
import os
import inspect
import uuid

import httpx

from xuloom.core.config import load_config


def model_error_message(error):
    """Do not expose response bodies, credentials or signed URLs in API failures."""
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return f"模型服务认证或权限失败（HTTP {status}）；请检查 Key、账号权限和 API 地址。已有结果保留。"
    if status == 429:
        return "模型服务限流或额度不足（HTTP 429）；请检查额度与连接限流设置，稍后继续。已有结果保留。"
    if status == 400:
        # Classify documented errors, but never echo untrusted bodies or URLs.
        detail = str(error).lower()
        if "tool_choice" in detail and "thinking" in detail and "support" in detail:
            return "模型服务拒绝请求（HTTP 400）：思考模式不支持当前 tool_choice 强制工具设置。请更新程序后继续；已有结果保留。"
        if "reasoning_content" in detail and "pass" in detail:
            return "模型服务拒绝请求（HTTP 400）：接口要求回传 reasoning_content，当前历史可能缺少协议记录。请更新程序；旧记录无法补回时可新建会话，已有结果保留。"
    if status in (400, 404, 422):
        return f"模型服务拒绝请求（HTTP {status}）；请核对模型名、接口协议及支持的参数。已有结果保留。"
    if isinstance(status, int) and status >= 500:
        return f"模型服务暂时不可用（HTTP {status}）；请稍后继续。已有结果保留。"
    if isinstance(error, ImportError):
        return "运行依赖无法加载；桌面用户请重新解压完整程序包，源码环境请检查安装依赖。已有记录保留。"
    if isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"}:
        return "模型连接失败或超时；请检查服务地址、网络和代理。已有结果保留，不会自动重放未确认操作。"
    return f"执行异常（{type(error).__name__}）；请查看活动记录并核对已有结果后继续。"


def model_network_mode(value=None):
    mode = value if value is not None else os.environ.get("XULOOM_NETWORK_MODE") or load_config().get("network_mode", "direct")
    if mode not in {"direct", "system"}:
        raise ValueError("Unknown model network mode")
    return mode


def model_http_client(client_type=None, *, network_mode=None, flow=None, request_owner=None):
    # httpx also reads the Windows registry when trust_env=True. A system
    # proxy can silently break otherwise reachable local/custom model APIs.
    if flow is not None:
        from xuloom.core.request_flow import http_client_type
        client_type = http_client_type(client_type or httpx.AsyncClient, flow, request_owner or uuid.uuid4().hex)
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


def create_model_client(*, provider, model, api_base_url, api_key, network_mode, request_owner=None):
    """One explicit client factory for generation and context compression.

    The provider names a registered :class:`xuloom.models.ModelAdapter`; an
    empty provider keeps the historical Anthropic default.
    """
    if not model:
        return None  # Unconfigured sessions need neither an SDK nor a connection.
    from xuloom.models.registry import get_adapter
    adapter = get_adapter(provider or "anthropic")
    if adapter is None:
        raise ValueError("Unsupported model provider")
    from xuloom.security.credentials import api_key as checked_key
    key = checked_key(api_key or ("not-configured" if not model else ""))
    from xuloom.core.request_flow import RequestFlow, connection_id
    flow = RequestFlow(connection_id(api_base_url or adapter.default_base_url, key), lambda: load_config(decrypt_secrets=False).get("model_request_limits"))
    client_type = adapter.sdk_http_client_type() if hasattr(adapter, "sdk_http_client_type") else None
    http_client = model_http_client(client_type, network_mode=network_mode, flow=flow, request_owner=request_owner)
    if client_type is None:
        # Plain HTTP adapters get the default timeout instead of an SDK's own.
        http_client.timeout = httpx.Timeout(600, connect=10)
    from xuloom.models.registry import bind_client
    return bind_client(adapter.create_client(model=model, api_base_url=api_base_url, api_key=key, http_client=http_client), adapter)
