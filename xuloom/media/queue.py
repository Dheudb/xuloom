"""Thin fal queue transport: no submission retries or credential-bearing downloads."""
import json
from urllib.parse import urlsplit

import httpx

from xuloom.media.config import address, origin

JSON_LIMIT = 2 * 1024 * 1024


def queue_url(binding, url):
    address(url)
    if origin(url) != origin(binding["base_url"]) or not urlsplit(url).path.startswith(urlsplit(binding["base_url"]).path.rstrip("/") + "/"):
        raise ValueError("Provider returned a queue address outside the configured API")
    return url


def download_url(binding, url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Invalid media download URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Invalid media download URL")
    host = parsed.hostname.lower()
    candidate = parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)
    trusted = {origin(binding["base_url"]), *(origin(item) for item in binding["download_origins"])}
    fal_cdn = candidate[0] == "https" and candidate[2] == 443 and (host == "fal.media" or host.endswith(".fal.media") or host == "storage.googleapis.com")
    openai_cdn = candidate[0] == "https" and candidate[2] == 443 and host.endswith(".blob.core.windows.net")
    if candidate not in trusted and not fal_cdn and not openai_cdn:
        raise ValueError("Media download host is not authorized; add its origin to this connection explicitly")
    return url


def client(binding):
    return httpx.AsyncClient(trust_env=binding["network_mode"] == "system", follow_redirects=False,
        timeout=httpx.Timeout(60, connect=10))


async def request(binding, key, method, url, payload=None):
    queue_url(binding, url)
    # No ambient model credentials, SDK profiles, retries or cross-origin redirects.
    async with client(binding) as transport:
        async with transport.stream(method, url, json=payload, headers={"Authorization": "Key " + key,
                "X-Fal-No-Retry": "1", "x-app-fal-disable-fallback": "true", "Accept": "application/json"}) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > JSON_LIMIT:
                    raise ValueError("Provider JSON exceeds 2 MB; result was not treated as complete")
            if response.is_error:
                try:
                    detail = json.loads(body).get("detail")
                    if isinstance(detail, list):
                        detail = "; ".join(".".join(str(part) for part in row.get("loc", []) if isinstance(part, (str, int)))[:120] + ": " + str(row.get("msg", "Invalid input"))[:200]
                            for row in detail[:6] if isinstance(row, dict))
                    if isinstance(detail, str):
                        response.extensions["media_validation_error"] = detail.replace(key, "[redacted]")[:600]
                except (ValueError, AttributeError, TypeError):
                    pass
            response.raise_for_status()
            value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Provider must return a JSON object")
    return value


def submit_receipt(binding, value):
    identity = value.get("request_id")
    import re
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise ValueError("Provider did not return a valid request ID; submission outcome is unknown")
    urls = {field: queue_url(binding, value.get(field)) for field in ("status_url", "response_url", "cancel_url")}
    # All three URLs must name the returned job, not another account operation.
    for field, url in urls.items():
        suffix = "/requests/" + identity + {"status_url": "/status", "response_url": "", "cancel_url": "/cancel"}[field]
        if not urlsplit(url).path.endswith(suffix):
            raise ValueError("Provider request ID and operation URLs disagree")
    return {"request_id": identity, **urls}


def output_files(binding, result):
    kind = binding["model"]["kind"]
    if kind in {"image", "image-edit"}:
        rows = result.get("images", [result["image"]] if isinstance(result.get("image"), dict) else [])
    else:
        item = result.get("video" if kind == "video" else "audio")
        rows = [item] if isinstance(item, dict) else []
    if not isinstance(rows, list) or not rows or len(rows) > 8:
        raise ValueError("Provider returned no valid media outputs (or more than eight)")
    files = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid provider output descriptor")
        url = download_url(binding, row.get("url"))
        size = row.get("file_size")
        if size is not None and (type(size) is not int or size < 1):
            raise ValueError("Invalid provider file size")
        files.append({"url": url, "size": size})
    return files
