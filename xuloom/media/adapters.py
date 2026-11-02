"""Select a known wire contract before the single generation POST.

No probing by generation, fallback submission, implicit provider/model, or
credential forwarding to discovery/CDN origins.
"""
import base64
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from xuloom.media import queue

PROTOCOLS = ("fal-queue", "openai-media", "eleven-audio")


async def public_schema(binding, url):
    try:
        async with queue.client(binding) as client:
            async with client.stream("GET", url, timeout=httpx.Timeout(10, connect=5)) as response:
                if response.status_code != 200:
                    return None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > queue.JSON_LIMIT:
                        raise ValueError("Generation API schema exceeds 2 MB")
                value = json.loads(data)
                return value if isinstance(value, dict) and isinstance(value.get("paths"), dict) else None
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def api_root(url):
    for suffix in ("/images/generations", "/images/edits", "/audio/speech", "/videos", "/sound-generation", "/music"):
        if url.endswith(suffix):
            return url[:-len(suffix)], suffix
    match = re.search(r"/text-to-speech(?:/[^/]+)?$", url)
    return (url[:match.start()], match.group()) if match else (url, "")


async def json_request(binding, key, method, url, *, protocol, payload=None, multipart=False, uploads=None):
    body, mime = await request(binding, key, method, url, protocol=protocol, payload=payload,
        multipart=multipart, uploads=uploads, maximum=32 * 1024 * 1024)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Generation API must return a JSON object")
    return value


async def request(binding, key, method, url, *, protocol, payload=None, multipart=False,
                  maximum=256 * 1024 * 1024, uploads=None):
    root, _ = api_root(binding["base_url"])
    scope = {**binding, "base_url": root}
    queue.queue_url(scope, url)
    headers = {"xi-api-key": key} if protocol == "eleven-audio" else {"Authorization": "Bearer " + key}
    kwargs = {"files": [(name, (None, str(value))) for name, value in (payload or {}).items()] + (uploads or [])} if multipart else {"json": payload} if payload is not None else {}
    async with queue.client(binding) as transport:
        async with transport.stream(method, url, headers=headers, **kwargs) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError("Generation response exceeds its size limit")
            if response.is_error:
                # Provider body may echo prompts, credentials, or arbitrary HTML.
                # Preserve status without reflecting those values into chat.
                response.raise_for_status()
            return bytes(data), response.headers.get("content-type", "").partition(";")[0].lower()


async def detect(binding):
    if binding["protocol"] != "auto":
        return binding["protocol"], None
    url = binding["base_url"]
    host = urlsplit(url).hostname.lower()
    root, suffix = api_root(url)
    kind = binding["model"]["kind"]
    if host == "queue.fal.run":
        schema = await public_schema(binding, "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=" + quote(binding["model"]["model"], safe=""))
        return "fal-queue", schema
    if host == "api.elevenlabs.io" or suffix in {"/sound-generation", "/music"} or suffix.startswith("/text-to-speech"):
        if kind not in {"speech", "sound", "music"}:
            raise ValueError("This audio API cannot generate images or videos")
        expected = "sound" if suffix == "/sound-generation" else "music" if suffix == "/music" else "speech" if suffix.startswith("/text-to-speech") else kind
        if kind != expected:
            raise ValueError("API address does not match the selected audio generation type")
        return "eleven-audio", None
    if suffix in {"/images/generations", "/images/edits", "/audio/speech", "/videos"}:
        expected = "image" if suffix.startswith("/images/") else "speech" if suffix == "/audio/speech" else "video"
        if kind not in ({"image", "image-edit"} if expected == "image" else {expected}):
            raise ValueError("API address does not match the selected generation type")
        return "openai-media", None
    # A custom service can advertise a known protocol through public OpenAPI.
    # This GET never sends a key and does not submit a generation.
    schema = await public_schema(binding, root + "/openapi.json")
    paths = schema.get("paths", {}) if schema else {}
    if any("/requests/" in p and p.endswith("/status") for p in paths):
        return "fal-queue", schema
    if any(p.endswith("/sound-generation") or p.endswith("/music") or "/text-to-speech/" in p for p in paths):
        if kind in {"speech", "sound", "music"}:
            return "eleven-audio", schema
    if kind in {"image", "image-edit", "video", "speech"}:
        return "openai-media", schema
    raise ValueError("无法识别此音效／音乐接口。请填写实际生成 API 的完整地址；目前适配队列接口和已支持的音频接口，不会试投生成来猜测协议。")


def options(values):
    allowed = {"size", "aspect_ratio", "duration_seconds", "voice", "speed", "seed", "instrumental", "negative_prompt", "n"}
    if values.keys() - allowed:
        raise ValueError("Use the standard generation options, not provider-specific JSON: " + ", ".join(sorted(values.keys() - allowed)))
    if "size" in values and (not isinstance(values["size"], str) or not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|auto", values["size"])):
        raise ValueError("size must be WIDTHxHEIGHT or auto")
    if "aspect_ratio" in values and values["aspect_ratio"] not in {"1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3"}:
        raise ValueError("Unsupported aspect ratio")
    for key, lo, hi in [("duration_seconds", .5, 600), ("speed", .25, 4), ("seed", 0, 2147483647), ("n", 1, 8)]:
        if key in values and (type(values[key]) not in {int, float} or not lo <= values[key] <= hi):
            raise ValueError("Invalid generation option: " + key)
    if "n" in values and type(values["n"]) is not int or "seed" in values and type(values["seed"]) is not int:
        raise ValueError("n and seed must be integers")
    if "instrumental" in values and type(values["instrumental"]) is not bool:
        raise ValueError("instrumental must be true or false")
    for key in ("voice", "negative_prompt"):
        if key in values and (not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > 2000):
            raise ValueError("Invalid generation option: " + key)
    return values


def endpoint(binding, protocol, *, edit=False):
    root, suffix = api_root(binding["base_url"])
    kind = binding["model"]["kind"]
    if protocol == "openai-media":
        return root + (("/images/edits" if edit or kind == "image-edit" else "/images/generations") if kind in {"image", "image-edit"} else "/audio/speech" if kind == "speech" else "/videos")
    if protocol == "eleven-audio":
        return root + (suffix if suffix.startswith("/text-to-speech/") else "/text-to-speech" if kind == "speech" else "/sound-generation" if kind == "sound" else "/music")
    return binding["base_url"] + "/" + binding["model"]["model"]


async def prepare(binding, key, protocol, prompt, values, images):
    values = options(values)
    kind = binding["model"]["kind"]
    model = binding["model"]["model"]
    url = endpoint(binding, protocol, edit=bool(images))
    uploads = []
    if protocol == "openai-media":
        if kind in {"sound", "music"}:
            raise ValueError("This protocol has no standard sound-effect/music endpoint")
        if kind in {"image", "image-edit"}:
            if binding["base_url"].endswith("/images/edits") and not images:
                raise ValueError("The image edit endpoint requires a reference image")
            accepted = {"size", "aspect_ratio", "n"}
            payload = {"model": model, "prompt": prompt, "n": values.get("n", 1)}
            if values.get("size"):
                payload["size"] = values["size"]
            elif values.get("aspect_ratio"):
                payload["size"] = "1536x1024" if values["aspect_ratio"] in {"16:9", "4:3", "3:2"} else "1024x1536" if values["aspect_ratio"] != "1:1" else "1024x1024"
            uploads = [("image[]", ("reference-" + str(i) + ".png", image["data"], image["mime"])) for i, image in enumerate(images)]
        elif kind == "speech":
            if images: raise ValueError("The speech endpoint does not accept reference images")
            accepted = {"voice", "speed"}
            payload = {"model": model, "input": prompt, "voice": values.get("voice", "alloy"), "response_format": "wav"}
            if "speed" in values: payload["speed"] = values["speed"]
        else:
            accepted = {"size", "duration_seconds", "aspect_ratio"}
            payload = {"model": model, "prompt": prompt}
            if "size" in values: payload["size"] = values["size"]
            elif "aspect_ratio" in values: payload["size"] = "720x1280" if values["aspect_ratio"] in {"9:16", "3:4", "2:3"} else "1280x720"
            if "duration_seconds" in values:
                if values["duration_seconds"] not in {4, 8, 12}: raise ValueError("This video protocol supports 4, 8 or 12 seconds")
                payload["seconds"] = str(int(values["duration_seconds"]))
            if len(images) > 1: raise ValueError("This video endpoint accepts one reference image")
            uploads = [("input_reference", ("reference.png", image["data"], image["mime"])) for image in images]
    else:
        if images: raise ValueError("This audio endpoint does not accept reference images")
        payload = {"model_id": model, "prompt" if kind == "music" else "text": prompt}
        accepted = {"duration_seconds", "instrumental"} if kind == "music" else {"duration_seconds"} if kind == "sound" else {"voice"}
        if kind == "music":
            if "duration_seconds" in values: payload["music_length_ms"] = int(values["duration_seconds"] * 1000)
            if "instrumental" in values: payload["force_instrumental"] = values["instrumental"]
        elif kind == "sound":
            if values.get("duration_seconds", 0) > 30: raise ValueError("Sound effects are limited to 30 seconds by this protocol")
            if "duration_seconds" in values: payload["duration_seconds"] = values["duration_seconds"]
        elif not re.search(r"/text-to-speech/[^/]+$", url):
            voice = values.get("voice")
            if not voice:
                root, _ = api_root(binding["base_url"])
                catalog = await json_request(binding, key, "GET", root + "/voices", protocol=protocol)
                voices = catalog.get("voices", [])
                voice = next((v.get("voice_id") for v in voices if isinstance(v, dict) and isinstance(v.get("voice_id"), str)), None)
            if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice):
                raise ValueError("No available voice was returned by the configured audio service")
            url += "/" + quote(voice, safe="")
    if values.keys() - accepted:
        raise ValueError("This generation endpoint does not support: " + ", ".join(sorted(values.keys() - accepted)))
    return url, payload, uploads


async def fal_payload(binding, prompt, values, images, schema=None):
    """Known input names come from the model schema when available."""
    values = options(values)
    model = binding["model"]
    properties, required = {}, []
    if schema:
        # Resolve only local references. Remote schema URLs are never fetched.
        def resolve(value):
            for _ in range(8):
                ref = value.get("$ref") if isinstance(value, dict) else None
                if not ref: return value
                if not ref.startswith("#/"): raise ValueError("Remote model schema references are not supported")
                value = schema
                for part in ref[2:].split("/"): value = value.get(part.replace("~1", "/").replace("~0", "~"), {})
            raise ValueError("Recursive model input schema")
        paths = schema.get("paths", {})
        selected = paths.get("/" + model["model"])
        for path in ([selected] if selected else paths.values()):
            body = resolve(path.get("post", {}).get("requestBody", {}))
            shape = resolve(body.get("content", {}).get("application/json", {}).get("schema", {}))
            if shape.get("properties"):
                properties, required = shape["properties"], shape.get("required", [])
                break
    field = next((f for f in ("prompt", "text", "input") if f in properties), None) or ("text" if model["kind"] == "speech" else "prompt")
    payload = {field: prompt}
    aliases = {"duration_seconds": ("duration_seconds", "duration", "seconds_total"), "size": ("image_size", "size"), "voice": ("voice_id", "voice"),
        "n": ("num_images", "n"), "instrumental": ("instrumental", "force_instrumental")}
    for name, value in values.items():
        target = next((p for p in aliases.get(name, (name,)) if p in properties), None) if properties else aliases.get(name, (name,))[0]
        if name == "aspect_ratio" and not target and "image_size" in properties:
            target = "image_size"
            value = {"1:1": "square_hd", "4:3": "landscape_4_3", "3:4": "portrait_4_3", "16:9": "landscape_16_9", "9:16": "portrait_16_9"}.get(value)
            if not value: raise ValueError("This model does not expose that aspect ratio")
        if not target: raise ValueError("The model schema does not support option: " + name)
        shape = properties.get(target, {})
        if name == "size" and value == "auto": continue
        if target == "image_size" and name == "size" and value != "auto":
            width, height = value.split("x"); value = {"width": int(width), "height": int(height)}
        if shape.get("type") == "string" and type(value) in {int, float}: value = str(value)
        if "enum" in shape and value not in shape["enum"]: raise ValueError("Unsupported model option: " + name)
        if type(value) in {int, float} and ("minimum" in shape and value < shape["minimum"] or "maximum" in shape and value > shape["maximum"]):
            raise ValueError("Model option is outside its supported range: " + name)
        payload[target] = value
    if images:
        data = ["data:" + image["mime"] + ";base64," + base64.b64encode(image["data"]).decode() for image in images]
        field = next((f for f in ("image_urls", "image_url", "first_frame_url") if f in properties), None)
        if properties and not field: raise ValueError("This configured model does not accept reference images")
        field = field or "image_urls"
        if field != "image_urls" and len(data) != 1: raise ValueError("This model accepts only one reference image")
        payload[field] = data if field == "image_urls" else data[0]
    for name in required:
        if name not in payload:
            default = properties.get(name, {}).get("default")
            if default is None:
                raise ValueError("Model requires " + name + "; describe this option in your prompt or use a supported model")
            payload[name] = default
    return payload
