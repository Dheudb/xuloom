"""Bounded, credential-safe web diagnostics and per-client DDGS networking."""
import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass_environment

import httpx

from ddgs.exceptions import DDGSException as SearchError, TimeoutException as SearchTimeout, RatelimitException as SearchRateLimit


def safe_detail(value, secrets=()):
    text = str(value)
    def safe_url(match):
        try:
            url = urlsplit(match.group())
            host = url.hostname or ""
            if ":" in host: host = "[" + host + "]"
            if url.port: host += ":" + str(url.port)
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[redacted URL]"
    text = re.sub(r"(?:https?|socks5h?)://[^\s<>\"']+", safe_url, text, flags=re.I)
    text = re.sub(r"(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|proxy-authorization|password|secret)\b[\"']?\s*[:=]\s*[\"']?)(?:(?:Bearer|Basic)\s+)?[^\s,;\"'}]+", r"\1[redacted]", text, flags=re.I)
    text = re.sub(r"\b(?:Bearer|Basic)\s+[^\s,;\"']+", "[redacted authorization]", text, flags=re.I)
    for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted key]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)[:1000]


def network_error(error, metadata, secrets=()):
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 3:
        chain.append(current); seen.add(id(current))
        current = (current.__cause__ or (current.__context__ if not current.__suppress_context__ else None)
                   or next((arg for arg in current.args if isinstance(arg, BaseException)), None))
    message = " | ".join(str(e)[:6000] for e in chain)
    response = next((e.response for e in chain if isinstance(e, httpx.HTTPStatusError)), None)
    status = response.status_code if response is not None else None
    if status is None:
        match = re.search(r"\b(?:HTTP(?:/\d(?:\.\d)?)?(?:\s+status)?|status(?:_code| code)?)\s*[:=]?\s*(\d{3})\b", message, re.I)
        if match: status = int(match[1])
    lower = message.lower()
    if status == 407:
        code = "proxy_authentication_error"
    elif status == 429 or any(isinstance(e, SearchRateLimit) for e in chain) or re.search(r"rate.?limit|too many requests", lower):
        code = "rate_limited"
    elif status in {401, 403}:
        code = "access_denied"
    elif status is not None:
        code = "http_error"
    elif re.search(r"certificate|cert_verify|unknown issuer|tls handshake|ssl verification", lower):
        code = "tls_error"
    elif any(isinstance(e, (TimeoutError, httpx.TimeoutException, SearchTimeout)) for e in chain) or re.search(r"timed? ?out|timeoutexception", lower):
        code = "timeout"
    elif re.search(r"no (?:usable )?results?(?: found)?[.!]?$", lower):
        code = "no_results"
    elif re.search(r"query is mandatory|query (?:is )?(?:empty|invalid)", lower):
        code = "invalid_query"
    elif re.search(r"dns|name resolution|resolve host|resolve hostname|nodename|name or service not known", lower):
        code = "dns_error"
    elif any(isinstance(e, httpx.ProxyError) for e in chain) or re.search(r"proxy.*(?:connect|refus|unreach)|tunnel.*fail", lower):
        code = "proxy_error"
    elif any(isinstance(e, (ConnectionError, httpx.NetworkError)) for e in chain) or re.search(r"connection.*(?:reset|refus|closed|aborted)|error sending request|network unreachable", lower):
        code = "connection_error"
    elif isinstance(error, ValueError):
        code = "configuration_error"
    else:
        code = "provider_error"
    retryable = (status in {408, 425, 429} or status is not None and status >= 500
                 or code in {"timeout", "dns_error", "proxy_error", "connection_error", "rate_limited"}
                 or code == "provider_error" and any(isinstance(e, SearchError) for e in chain))
    detail = safe_detail(" → ".join(type(e).__name__ + ": " + str(e) for e in chain), secrets)
    result = {**metadata, "reason": type(error).__name__, "code": code, "http_status": status,
              "retryable": bool(retryable), "detail": detail}
    if response is not None:
        header = response.headers.get("retry-after", "")
        try:
            try:
                delay = float(header)
            except ValueError:
                delay = max(0, parsedate_to_datetime(header).timestamp() - time.time())
            if math.isfinite(delay) and delay >= 0: result["retry_after_seconds"] = delay
        except (ValueError, TypeError, OverflowError):
            pass
    return result


def search_network(mode):
    from xuloom.core.network import model_network_mode
    mode = model_network_mode(mode)
    proxies = getproxies() if mode == "system" else {}
    return mode, proxies, {"network_mode": mode, "proxy_configured": bool(proxies.get("https") or proxies.get("all")), "proxy_used": False}


def proxy_for_url(proxies, url):
    target = urlsplit(str(url))
    if proxy_bypass_environment(target.netloc, proxies): return None
    proxy = proxies.get(target.scheme) or proxies.get("all")
    if not proxy: return None
    proxy = proxy if "://" in proxy else "http://" + proxy
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Invalid system proxy configuration; check the configured proxy address")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid system proxy port") from None
    return proxy


class _RoutedClient:
    def __init__(self, client, proxies, info):
        self.client, self.proxies, self.info = client, proxies, info
        self.last_proxy = object()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, url, *args, **kwargs):
        proxy = proxy_for_url(self.proxies, url)
        # Assigning None explicitly disables primp's ambient environment proxy.
        # No process-wide environment changes, even during parallel searches.
        if proxy != self.last_proxy:
            self.client.proxy = proxy
            self.last_proxy = proxy
        if proxy: self.info["proxy_used"] = True
        response = self.client.request(method, url, *args, **kwargs)
        # DDGS engines can otherwise turn HTTP failures into empty results.
        # Preserve only status/cooldown, never an upstream error body or cookies.
        if response.status_code >= 400:
            request = httpx.Request(method, str(url))
            retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
            headers = {"Retry-After": retry_after} if retry_after else {}
            retained = httpx.Response(response.status_code, headers=headers, request=request)
            raise httpx.HTTPStatusError(f"Search endpoint {safe_detail(url)} returned HTTP {response.status_code}", request=request, response=retained)
        return response


def run_ddgs(factory, query, limit, proxies, info, timeout):
    proxy = proxy_for_url(proxies, "https://search.invalid")
    search = factory(proxy=proxy, timeout=timeout)
    search._proxy = proxy  # DDGS 9.x otherwise falls back to DDGS_PROXY.
    original = search._get_engines
    def engines(*args, **kwargs):
        rows = original(*args, **kwargs)
        for row in rows:
            if not isinstance(row.http_client.client, _RoutedClient):
                row.http_client.client = _RoutedClient(row.http_client.client, proxies, info)
        return rows
    search._get_engines = engines
    return list(search.text(query, max_results=limit))


def diagnostic_secrets(options, proxies):
    secrets = [options.get("api_key", "")]
    for value in proxies.values():
        if not isinstance(value, str) or "://" not in value: continue
        try:
            parsed = urlsplit(value)
            if parsed.password: secrets.extend([parsed.password, unquote(parsed.password)])
        except ValueError:
            continue
    return secrets
