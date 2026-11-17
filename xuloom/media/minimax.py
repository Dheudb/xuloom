"""MiniMax H3 video and synchronous speech; reuse durable media receipts."""
import asyncio
import base64
import json
import re
import time
from urllib.parse import urlsplit

from xuloom.core.storage import atomic_bytes
from xuloom.media import adapters, files, jobs

HOSTS = {"api.minimax.cn", "api.minimax.io", "api.minimaxi.com", "api.minimax.chat"}
NATIVE_PATHS = ("/v1/t2a_v2", "/v2/video_generation")
VIDEO_MODELS = {"MiniMax-H3", "MiniMax-H3-Highspeed", "MiniMax-H3-Lite", "MiniMax-H3-Max"}


def matches(binding):
    parsed = urlsplit(binding["base_url"])
    return binding["model"]["kind"] in {"video", "speech"} and (
        parsed.hostname in HOSTS or any(parsed.path.endswith(p) for p in NATIVE_PATHS))


def root(binding):
    url = binding["base_url"].rstrip("/")
    for suffix in (*NATIVE_PATHS, "/v1", "/v2"):
        if url.endswith(suffix):
            return url[:-len(suffix)]
    if not urlsplit(url).path:
        return url
    raise ValueError("MiniMax media requires its API root or the matching native generation endpoint")


def endpoint(binding):
    kind = binding["model"]["kind"]
    suffix = "/v1/t2a_v2" if kind == "speech" else "/v2/video_generation"
    if any(binding["base_url"].endswith(p) and p != suffix for p in NATIVE_PATHS):
        raise ValueError("MiniMax API address does not match this generation type")
    return root(binding) + suffix


def prepare(binding, prompt, values, images):
    kind, model = binding["model"]["kind"], binding["model"]["model"]
    url = endpoint(binding)
    if kind == "speech":
        if values.keys() - {"voice", "speed"} or images:
            raise ValueError("MiniMax speech accepts voice and speed; no reference images")
        if len(prompt) > 10000 or not .5 <= values.get("speed", 1) <= 2:
            raise ValueError("MiniMax speech supports up to 10000 characters and speed 0.5–2")
        return url, {"model": model, "text": prompt, "stream": False, "output_format": "hex",
            "voice_setting": {"voice_id": values.get("voice", "Chinese (Mandarin)_Lyrical_Voice"),
                "speed": values.get("speed", 1)},
            "audio_setting": {"format": "wav", "sample_rate": 32000, "channel": 1}}, []
    if kind != "video" or model not in VIDEO_MODELS:
        raise ValueError("This MiniMax video adapter supports H3 models; legacy Hailuo uses a different API")
    if values.keys() - {"duration_seconds", "aspect_ratio"}:
        raise ValueError("MiniMax H3 currently supports duration_seconds and aspect_ratio at 768P")
    seconds = values.get("duration_seconds", 5)
    if type(seconds) is not int or not (5 if model == "MiniMax-H3-Max" else 4) <= seconds <= 15:
        raise ValueError("MiniMax H3 duration must be an integer from 4 to 15 (Max: 5 to 15)")
    ratio = values.get("aspect_ratio", "16:9")
    if ratio not in {"1:1", "4:3", "3:4", "16:9", "9:16"}:
        raise ValueError("Unsupported MiniMax H3 aspect ratio")
    content = [{"type": "text", "text": prompt}]
    for image in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:" + image["mime"] + ";base64," + base64.b64encode(image["data"]).decode()},
            "role": "reference_image"})
    return url, {"model": model, "content": content, "resolution": "768P",
        "duration": seconds, "ratio": ratio}, []


async def finish_speech(tool, record, body):
    value = json.loads(body)
    status = value.get("base_resp", {}).get("status_code") if isinstance(value, dict) else None
    if type(status) is not int:
        raise ValueError("MiniMax speech response has no confirmed status")
    if status:
        record.update(phase="failed", error="MiniMax speech service rejected the request (code " + str(status) + ")")
        tool._save(record)
        return tool._response(record)
    data = value.get("data")
    audio = data.get("audio") if isinstance(data, dict) and data.get("status") == 2 else None
    if not isinstance(audio, str) or not audio or len(audio) > files.MEDIA_LIMIT * 2 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", audio):
        raise ValueError("MiniMax speech response contains no complete hex audio")
    source = jobs.directory(tool.context) / (record["id"] + ".audio")
    record["phase"] = "downloading"
    tool._save(record)
    atomic_bytes(source, bytes.fromhex(audio))
    try:
        file = files.publish(source, record["output_dir"], record["id"] + "-1", "speech", "audio/wav")
    finally:
        source.unlink(missing_ok=True)
    from xuloom.features.file_changes import record_written_file
    record_written_file(tool.context, file["path"], None, "generate")
    record.update(phase="downloaded", files=[file])
    record.pop("error", None)
    tool._save(record)
    return tool._response(record)


async def submit_video(tool, record, key, url, payload):
    value = await adapters.json_request(record["binding"], key, "POST", url, protocol="minimax-media", payload=payload)
    identity = value.get("task_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
        raise ValueError("MiniMax returned no valid task ID; submission outcome is unknown")
    record.update(request_id=identity, phase="submitted")
    tool._save(record)


async def inspect_video(tool, record, key, action, wait_ms):
    from xuloom.media import generation
    identity = record.get("request_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
        record.update(phase="unknown", error="MiniMax task ID is unavailable; no new generation was submitted")
        tool._save(record)
        return tool._response(record)
    binding, deadline = record["binding"], time.monotonic() + wait_ms / 1000
    url = root(binding) + "/v2/query/video_generation/" + identity
    while True:
        response = await adapters.json_request(binding, key, "GET", url, protocol="minimax-media")
        task = response.get("task")
        if not isinstance(task, dict) or task.get("id") != identity or task.get("model", binding["model"]["model"]) != binding["model"]["model"]:
            raise ValueError("MiniMax returned a different video task")
        status = task.get("status")
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError("MiniMax returned an unknown video status")
        record["provider_status"] = status
        if status in {"failed", "cancelled"}:
            record.update(phase=status, error="MiniMax video task " + status)
            tool._save(record)
            return tool._response(record, cancel=action == "cancel")
        if status == "succeeded":
            link = (task.get("content") or {}).get("url")
            if not isinstance(link, str):
                raise ValueError("MiniMax completed without a video output")
            # Save before downloading; a local failure must not submit another job.
            generation.persist_response(tool, record, json.dumps({"data": [{"url": link}]}).encode(), "application/json")
            return await generation.finish(tool, record)
        record["phase"] = "submitted"
        record.pop("error", None)
        tool._save(record)
        if action == "cancel":
            # DELETE cancels queued tasks but deletes finished ones. A GET followed
            # by DELETE races with completion; the API offers no conditional cancel.
            record["error"] = "MiniMax cannot safely cancel here without risking deletion of a completed task. Check the provider; this task may continue."
            tool._save(record)
            result = tool._response(record)
            result.update(ok=False, is_error=True)
            return result
        if time.monotonic() >= deadline:
            return tool._response(record)
        await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
