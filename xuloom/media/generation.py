"""Sync and video results reuse the existing durable media job, not another queue."""
import asyncio
import base64
import hashlib
import json
import re
import time

from xuloom.core.storage import atomic_bytes
from xuloom.media import adapters, files, jobs


def persist_response(tool, record, body, mime):
    path = jobs.directory(tool.context) / (record["id"] + ".response")
    atomic_bytes(path, body)
    record.update(response_size=len(body), response_sha256=hashlib.sha256(body).hexdigest(), response_mime=mime,
        phase="result_pending")
    tool._save(record)


def response_bytes(tool, record):
    path = jobs.directory(tool.context) / (record["id"] + ".response")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Linked generation result storage is not allowed")
    if path.stat().st_size > files.MEDIA_LIMIT:
        raise ValueError("Saved generation result exceeds its size limit")
    body = path.read_bytes()
    if len(body) != record.get("response_size") or hashlib.sha256(body).hexdigest() != record.get("response_sha256"):
        raise ValueError("Saved generation result changed; no generation was repeated")
    return path, body


async def finish(tool, record):
    source, body = response_bytes(tool, record)
    binding, completed = record["binding"], []
    kind, mime = binding["model"]["kind"], record["response_mime"]
    descriptors = None
    if mime == "application/json":
        result = json.loads(body)
        descriptors = result.get("data")
        if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 8:
            raise ValueError("Generation API returned no complete image outputs")
    record["phase"] = "downloading"; tool._save(record)
    for index, descriptor in enumerate(descriptors or [None]):
        stem = record["id"] + "-" + str(index + 1)
        if descriptor is None:
            file = files.publish(source, record["output_dir"], stem, kind, mime)
        elif isinstance(descriptor, dict) and isinstance(descriptor.get("b64_json"), str):
            decoded = base64.b64decode(descriptor["b64_json"], validate=True)
            if len(decoded) > files.IMAGE_LIMIT: raise ValueError("Generated image exceeds 20 MB")
            temp = jobs.directory(tool.context) / (stem + ".image")
            atomic_bytes(temp, decoded)
            try: file = files.publish(temp, record["output_dir"], stem, kind)
            finally: temp.unlink(missing_ok=True)
        elif isinstance(descriptor, dict) and isinstance(descriptor.get("url"), str):
            file = await files.download(binding, {"url": descriptor["url"]}, record["output_dir"], stem)
        else:
            raise ValueError("Generation API returned an invalid output descriptor")
        completed.append(file); record["files"] = completed; tool._save(record)
        from xuloom.features.file_changes import record_written_file
        record_written_file(tool.context, file["path"], None, "generate")
    record.update(phase="downloaded", files=completed); record.pop("error", None); tool._save(record)
    return tool._response(record)


async def submit(tool, record, key, protocol, url, payload, uploads):
    binding = record["binding"]
    record["adapter"] = protocol
    tool._save(record)
    if protocol == "openai-media" and record["kind"] == "video":
        value = await adapters.json_request(binding, key, "POST", url, protocol=protocol, payload=payload, multipart=True, uploads=uploads)
        identity = value.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
            raise ValueError("Video API returned no valid task identity; submission outcome is unknown")
        record.update(request_id=identity, status_url=url + "/" + identity, response_url=url + "/" + identity + "/content", phase="submitted")
        tool._save(record)
        return None
    body, mime = await adapters.request(binding, key, "POST", url, protocol=protocol, payload=payload,
        multipart=bool(uploads), uploads=uploads)
    persist_response(tool, record, body, mime)
    return await finish(tool, record)


async def inspect(tool, record, key, action, wait_ms):
    if record.get("response_sha256"):
        return await finish(tool, record)
    if record.get("adapter") != "openai-media" or record["kind"] != "video" or not record.get("status_url"):
        record.update(phase="unknown", error="This synchronous request has no saved complete response; inspect the provider before submitting again.")
        tool._save(record); return tool._response(record)
    if action == "cancel":
        record["error"] = "此视频接口没有已确认的取消操作；没有把删除产物当作停止生成。请在供应商侧核对已有任务。"
        tool._save(record)
        value = tool._response(record); value.update(ok=False, is_error=True); return value
    identity = record.get("request_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
        raise ValueError("Invalid saved video identity")
    endpoint = adapters.endpoint(record["binding"], "openai-media") + "/" + identity
    if record["status_url"] != endpoint or record.get("response_url") != endpoint + "/content":
        raise ValueError("Saved video operation URLs no longer match its task")
    binding, deadline = record["binding"], time.monotonic() + wait_ms / 1000
    while True:
        value = await adapters.json_request(binding, key, "GET", record["status_url"], protocol="openai-media")
        if value.get("id") != record["request_id"]:
            raise ValueError("Video API returned a different task identity")
        status = value.get("status")
        if status not in {"queued", "in_progress", "completed", "failed"}:
            raise ValueError("Unknown video job status")
        record["provider_status"] = status
        if status == "failed":
            record.update(phase="failed", error="Provider video generation failed; no automatic resubmission")
            tool._save(record); return tool._response(record)
        if status == "completed":
            record["phase"] = "result_pending"; tool._save(record)
            body, mime = await adapters.request(binding, key, "GET", record["response_url"], protocol="openai-media")
            persist_response(tool, record, body, mime)
            return await finish(tool, record)
        record["phase"] = "submitted"; tool._save(record)
        if time.monotonic() >= deadline: return tool._response(record)
        await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
