"""Bounded, credential-safe web diagnostics and per-client DDGS networking."""
import math
import os
import hashlib
import ipaddress
from contextlib import AsyncExitStack
import re
import json
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit, urlunsplit, urljoin
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
    elif status == 451:
        code = "legal_restriction"
    elif status == 404:
        code = "not_found"
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


def web_network_mode(options=None):
    from xuloom.core.config import load_config
    mode = (options or {}).get("web_network_mode") or os.environ.get("XULOOM_WEB_NETWORK_MODE")
    if mode is None: mode = load_config(decrypt_secrets=False).get("web_network_mode", "auto")
    if not isinstance(mode, str) or mode not in {"auto", "direct", "system"}: raise ValueError("Unknown web network mode")
    return mode


def search_network(mode):
    mode = mode or web_network_mode()
    if not isinstance(mode, str) or mode not in {"auto", "direct", "system"}: raise ValueError("Unknown web network mode")
    proxies = getproxies() if mode != "direct" else {}
    return mode, proxies, {"network_mode": mode, "proxy_configured": bool(proxies.get("http") or proxies.get("https") or proxies.get("all")), "proxy_used": False}


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


def route_failure(error):
    return network_error(error, {})["code"] in {"timeout", "dns_error", "proxy_error", "connection_error", "proxy_authentication_error"}


class WebRoutes:
    """Read-only web routes, independent of model API routes; no credentials on disk."""
    def __init__(self, proxies, info):
        self.proxies, self.info = proxies, info

    def path(self, url):
        from xuloom.core.storage import data_directory, IOPath
        target = urlsplit(str(url))
        origin = f"{target.scheme}://{target.hostname}:{target.port or (443 if target.scheme == 'https' else 80)}"
        return IOPath(data_directory() / "web-routes" / (hashlib.sha256(origin.encode()).hexdigest() + ".json"))

    def routes(self, url):
        mode = self.info["network_mode"]
        if mode == "auto":
            host = urlsplit(str(url)).hostname or ""
            try: loopback = ipaddress.ip_address(host).is_loopback
            except ValueError: loopback = host.lower().rstrip(".") == "localhost"
            if loopback: return ["direct"]
        try: proxy = proxy_for_url(self.proxies, url)
        except ValueError as error:
            if mode != "auto": raise
            self.failed(url, "system", error)
            return ["direct"]
        if mode == "direct" or not proxy: return ["direct"]
        if mode == "system": return ["system"]
        try:
            previous = json.loads(self.path(url).read_text(encoding="utf-8")).get("route")
        except (OSError, ValueError, AttributeError): previous = None
        first = previous if isinstance(previous, str) and previous in {"direct", "system"} else "system"
        return [first, "direct" if first == "system" else "system"]

    def used(self, route):
        self.info["route_used"] = route
        if route == "system": self.info["proxy_used"] = True

    def failed(self, url, route, error):
        rows = self.info.setdefault("route_failures", [])
        if len(rows) < 16:
            rows.append({"url": safe_detail(url), "route": route, **network_error(error, {}, diagnostic_secrets({}, self.proxies))})

    def remember(self, url, route):
        if self.info["network_mode"] != "auto": return
        from xuloom.core.storage import atomic_json
        try:
            path = self.path(url)
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("route") == route: return
            except (OSError, ValueError, AttributeError): pass
            atomic_json(path, {"route": route})
        except OSError: self.info["route_cache_saved"] = False


class _RoutedClient:
    def __init__(self, client, proxies, info):
        self.client, self.proxies, self.info = client, proxies, info
        self.routing = WebRoutes(proxies, info)
        self.last_proxy = object()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, url, *args, **kwargs):
        routes = self.routing.routes(url)
        for index, route in enumerate(routes):
            proxy = proxy_for_url(self.proxies, url) if route == "system" else None
            self.routing.used(route)
            try:
                if proxy != self.last_proxy:
                    self.client.proxy = proxy
                    self.last_proxy = proxy
                response = self.client.request(method, url, *args, **kwargs)
                if response.status_code >= 400:
                    request = httpx.Request(method, str(url))
                    retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
                    retained = httpx.Response(response.status_code, headers={"Retry-After": retry_after} if retry_after else {}, request=request)
                    raise httpx.HTTPStatusError(f"Search endpoint {safe_detail(url)} returned HTTP {response.status_code}", request=request, response=retained)
            except Exception as error:
                self.routing.failed(url, route, error)
                if index + 1 == len(routes) or not route_failure(error): raise
                continue
            self.routing.remember(url, route)
            return response


def web_client(**kwargs):
    # Explicit proxy routing; keep configured TLS CA files without ambient proxy mounts.
    transport = httpx.AsyncHTTPTransport(proxy=kwargs.pop("proxy", None), trust_env=True)
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, **kwargs)


async def fetch_page(url, options, info, max_bytes, secrets):
    mode, proxies, network = search_network(web_network_mode(options))
    secrets.extend(diagnostic_secrets(options, proxies))
    info.update(network, requested_url=safe_detail(url), redirects=[])
    routing = WebRoutes(proxies, info)
    async with AsyncExitStack() as stack:
        clients = {}
        for hop in range(11):
            target = httpx.URL(url)
            if target.scheme not in {"http", "https"} or not target.host or target.username or target.password:
                raise ValueError("Redirect must use HTTP(S) without embedded credentials")
            info["final_url"] = safe_detail(str(target))
            routes = routing.routes(str(target))
            for index, route in enumerate(routes):
                routing.used(route)
                proxy = proxy_for_url(proxies, str(target)) if route == "system" else None
                try:
                    if proxy not in clients:
                        clients[proxy] = await stack.enter_async_context(web_client(proxy=proxy))
                    async with clients[proxy].stream("GET", str(target), timeout=httpx.Timeout(15, connect=5)) as response:
                        if response.is_redirect and response.headers.get("location"):
                            if hop == 10: raise httpx.TooManyRedirects("Webpage exceeded 10 redirects", request=response.request)
                            url = urljoin(str(response.url), response.headers["location"])
                            info["redirects"].append({"url": safe_detail(str(response.url)), "status": response.status_code, "to": safe_detail(url)})
                            routing.remember(str(target), route)
                            break
                        response.raise_for_status()
                        media = response.headers.get("content-type", "text/html").split(";", 1)[0].lower()
                        if not (media.startswith("text/") or media in {"application/xhtml+xml", "application/json", "application/xml"}):
                            return None, media, str(response.url), response.encoding
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > max_bytes: return None, "too_large", str(response.url), response.encoding
                            body.extend(chunk)
                    routing.remember(str(target), route)
                    return bytes(body), media, str(response.url), response.encoding
                except Exception as error:
                    routing.failed(str(target), route, error)
                    if index + 1 == len(routes) or not route_failure(error): raise
    raise RuntimeError("Webpage redirects did not complete")


def run_ddgs(factory, query, limit, proxies, info, timeout):
    try: proxy = proxy_for_url(proxies, "https://search.invalid")
    except ValueError:
        if info["network_mode"] != "auto": raise
        proxy = None
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
