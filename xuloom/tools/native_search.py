"""Native provider search behind the existing authorized web tools."""
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from xuloom.context.tool_results import build_tool_result

BACKENDS = {"builtin", "openai", "openai-chat", "anthropic", "deepseek", "qwen"}


def normalize(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or value.keys() - {"backend", "model", "baseUrl"}:
        raise ValueError("搜索配置只接受 backend、model、baseUrl")
    backend = value.get("backend", "builtin")
    model, base = value.get("model", ""), value.get("baseUrl", "")
    if backend not in BACKENDS or not isinstance(model, str) or len(model) > 256 or not isinstance(base, str):
        raise ValueError("无效的原生搜索配置")
    if base:
        parsed = urlsplit(base)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("搜索 API 地址必须为无凭据及查询参数的 HTTP(S) 根地址")
    return {"backend": backend, "model": model.strip(), "baseUrl": base.rstrip("/")}


def configured(provider, model, base, key):
    from xuloom.core.config import load_config, normalize_api_base_url
    from xuloom.core.model_catalog import active_connection, connections
    cfg = load_config()
    candidates = [row for row in connections(cfg) if row["provider"] == provider and model in row.get("models", [])
        and normalize_api_base_url(provider, row.get("base_url", "")) == normalize_api_base_url(provider, base or "")
        and row.get("api_key", "") == key]
    active = active_connection(cfg)
    selected = next((row for row in candidates if row["id"] == active["id"]), None)
    selected = selected or (candidates[0] if len(candidates) == 1 else None)
    return normalize((selected or {}).get("native_search"))


def endpoint(base, suffix):
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请配置有效的搜索 API 根地址；没有向默认供应商转发 Key")
    path = parsed.path.rstrip("/")
    if suffix.startswith("/tools/") and not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))


def source(url, title=""):
    if not isinstance(url, str) or any(ord(char) < 32 for char in url):
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    title = str(title or url).replace("[", "(").replace("]", ")").replace("\n", " ")[:500]
    return {"url": url, "title": title, "kind": "search"}


def extract(backend, body):
    texts, sources = [], []
    def add(item):
        if isinstance(item, dict):
            value = source(item.get("url"), item.get("title"))
            if value and value["url"] not in {row["url"] for row in sources}:
                sources.append(value)
    if backend == "deepseek":
        for item in body.get("results", []):
            add(item)
            texts.append(str(item.get("summary") or item.get("snippet") or ""))
    elif backend in {"openai", "qwen"}:
        for item in body.get("output", []):
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
                    for citation in part.get("annotations", []):
                        if citation.get("type") == "url_citation": add(citation)
            if item.get("type") == "web_search_call":
                for citation in item.get("action", {}).get("sources", []): add(citation)
    elif backend == "anthropic":
        for part in body.get("content", []):
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
                for citation in part.get("citations", []): add(citation)
            if part.get("type") == "web_search_tool_result" and isinstance(part.get("content"), list):
                for citation in part["content"]:
                    if citation.get("type") == "web_search_result": add(citation)
    else:
        for choice in body.get("choices", []):
            message = choice.get("message", {})
            texts.append(message.get("content") or "")
            for annotation in message.get("annotations", []):
                add(annotation.get("url_citation", annotation))
        for item in body.get("search_info", {}).get("search_results", []): add(item)
    return "\n\n".join(texts), sources


def account(context, body, backend, model):
    usage = body.get("usage") or {}
    inputs = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    outputs = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    if inputs < 0 or outputs < 0:
        raise ValueError("Invalid search usage")
    totals = context.get("session_token_usage")
    if isinstance(totals, dict):
        totals["input_tokens"] = totals.get("input_tokens", 0) + inputs
        totals["output_tokens"] = totals.get("output_tokens", 0) + outputs
        for key in ('cache_creation_input_tokens', 'cache_read_input_tokens'):
            totals[key] = totals.get(key, 0) + max(0, int(usage.get(key, 0) or 0))
    history = context.get("token_usage_history")
    if isinstance(history, list):
        history.insert(0, {"source": "native_search", "provider": backend, "model": model,
            "input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs})
        del history[50:]
    return usage


async def execute(context, query, limit, *, url=None):
    options = context["native_search_options"]()
    profile = normalize(options["profile"])
    backend, model = profile["backend"], profile["model"] or options["model"]
    base = profile["baseUrl"] or options["base_url"]
    if url and backend != "deepseek":
        return None  # Existing static fetch is independent of model-hosted search.
    headers = {"Authorization": "Bearer " + options["api_key"], "Content-Type": "application/json"}
    if backend == "deepseek":
        if not url and (not query.strip() or len(query) > 128):
            return build_tool_result(ok=False, is_error=True, content='DeepSeek search queries must contain 1–128 characters. Shorten this query without changing its meaning.',
                metadata={'backend': backend, 'code': 'invalid_query', 'retryable': False})
        target = endpoint(base, "/tools/fetch" if url else "/tools/search")
        payload = {"url": url} if url else {"query": query, "count": limit, "need_content": False}
    elif backend in {"openai", "qwen"}:
        target = endpoint(base, "/responses")
        payload = {"model": model, "input": query, "tools": [{"type": "web_search"}],
            "tool_choice": "required", "max_output_tokens": 2048, "include": ["web_search_call.action.sources"]}
        if backend == 'qwen':
            payload.pop('include')
            payload.pop('tool_choice')
    elif backend == "anthropic":
        target = endpoint(base, "/messages" if urlsplit(base).path.rstrip("/").endswith("/v1") else "/v1/messages")
        headers = {"x-api-key": options["api_key"], "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": 2048, "messages": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]}
    elif backend == "openai-chat":
        target = endpoint(base, "/chat/completions")
        payload = {"model": model, "messages": [{"role": "user", "content": query}], "max_tokens": 2048, "stream": False}
        payload["web_search_options"] = {}
    else:
        raise ValueError("Native search is not enabled")
    from xuloom.core.network import model_http_client
    from xuloom.core.request_flow import RequestFlow, connection_id
    from xuloom.core.config import load_config
    flow = RequestFlow(connection_id(base, options["api_key"]), lambda: load_config(decrypt_secrets=False).get("model_request_limits"))
    usages, responses = [], []
    async with model_http_client(network_mode=options["network_mode"], flow=flow, request_owner=options["owner"]) as client:
        async def post():
            response = await client.post(target, headers=headers, json=payload, timeout=httpx.Timeout(45, connect=10))
            response.raise_for_status()
            body = response.json()
            usages.append(account(context, body, backend, model))
            return body
        for _ in range(4):
            if backend == 'deepseek':
                body = await post()
            else:
                from types import SimpleNamespace
                from xuloom.core.output_limits import model_reservation
                async with model_reservation(SimpleNamespace(tool_context=context), 2048) as grant:
                    payload['max_tokens' if backend in {'anthropic', 'openai-chat'} else 'max_output_tokens'] = grant['maximum']
                    body = await post()
                    usage = body.get('usage') or {}
                    grant['reported'] = int(usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0)
            responses.append(body)
            if backend != "anthropic" or body.get("stop_reason") != "pause_turn": break
            payload["messages"].append({"role": "assistant", "content": body.get("content", [])})
        else:
            return build_tool_result(ok=False, is_error=True, content="Native search paused repeatedly; partial operations were retained. Do not assume no search occurred.",
                metadata={"backend": backend, "code": "search_incomplete", "usage": usages})
    metadata = {"backend": backend, "query": query, "usage": usages, "search_model": model,
        "sources_in_content": True, "requested_results": limit}
    if url:
        text = body.get("content")
        if not isinstance(text, str) or not text:
            return build_tool_result(ok=False, is_error=True, content="Native fetch returned no page content.", metadata={**metadata, "code": "no_results"})
        return build_tool_result(ok=True, content=text, metadata={**metadata, "sources": [source(url, body.get("title"))]})
    all_text, all_sources = [], []
    for body in responses:
        text, sources = extract(backend, body)
        if text: all_text.append(text)
        for item in sources:
            if item["url"] not in {row["url"] for row in all_sources}: all_sources.append(item)
    if not all_sources:
        return build_tool_result(ok=False, is_error=True, content="The selected native search returned no verifiable source URLs. Check API/model support; no other provider or built-in fallback was called.",
            metadata={**metadata, "code": "no_grounding", "result_count": 0})
    incomplete = (body.get('status') == 'incomplete' or body.get('stop_reason') == 'max_tokens'
        or any(choice.get('finish_reason') == 'length' for choice in body.get('choices', [])))
    content = "\n\n".join(all_text) + "\n\nSources:\n" + "\n".join("- [" + row["title"] + "](" + quote(row["url"], safe=':/?#[]@!$&*+,;=%~.-_') + ")" for row in all_sources)
    if incomplete:
        content = 'Search response incomplete; these are partial findings.\n\n' + content
    return build_tool_result(ok=not incomplete, is_error=incomplete, content=content,
        metadata={**metadata, 'code': 'search_incomplete' if incomplete else 'ok', "sources": all_sources, "result_count": len(all_sources)})
