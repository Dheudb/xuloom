"""Optional media generation through configured, durable provider queue requests."""
import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Literal, Optional
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from xuloom.context.tool_results import build_tool_result
from xuloom.core.run_result import PersistenceError
from xuloom.media import config, files, jobs, queue
from xuloom.tools.base import BaseAgenticTool


class MediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["capabilities", "list", "submit", "status", "cancel"]
    connection: Optional[str] = Field(None, description="Configured media connection ID, returned by capabilities")
    model: Optional[str] = Field(None, description="Model entry ID within the connection, not an invented provider endpoint")
    prompt: Optional[str] = Field(None, min_length=1, max_length=20000)
    inputs: dict = Field(default_factory=dict, description="Additional model input JSON, such as lyrics, voice_setting or duration; see the configured model defaults")
    image_paths: list[str] = Field(default_factory=list, max_length=4, description="Local reference images to upload for image-edit or image-conditioned generation")
    output_dir: Optional[str] = Field(None, description="Output directory for submit; defaults to generated/ in this workspace")
    job_id: Optional[str] = Field(None, description="Real Xuloom media task ID from submit/list; status never creates another generation")
    wait_ms: int = Field(30000, ge=0, le=120000, description="Maximum time to wait for this existing job, up to 120 seconds; returning early does not stop the provider")
    retry_unknown: bool = Field(False, description="Explicitly request another potentially billable submission after an identical request had unknown outcome; always requires a real user approval")

    @model_validator(mode="after")
    def required_fields(self):
        if self.action == "submit" and (not self.connection or not self.model or not self.prompt or not self.prompt.strip()):
            raise ValueError("submit requires connection, model and a non-empty prompt; discover capabilities first")
        if self.action in {"status", "cancel"} and not self.job_id:
            raise ValueError("status/cancel requires the existing job_id")
        if self.action != "submit" and (self.image_paths or self.inputs or self.output_dir or self.prompt or self.connection or self.model or self.retry_unknown):
            raise ValueError("Only submit accepts generation inputs; use job_id to inspect or cancel an existing task")
        return self


def _error(error):
    if isinstance(error, httpx.HTTPStatusError):
        detail = error.response.extensions.get("media_validation_error")
        return "Media service returned HTTP " + str(error.response.status_code) + (": " + detail if detail else "")
    if isinstance(error, httpx.HTTPError):
        return "Media connection failed (" + type(error).__name__ + "); use this job_id to inspect the existing request, do not resubmit"
    if isinstance(error, json.JSONDecodeError):
        return "Media service returned invalid JSON"
    return str(error)[:500]


class MediaTool(BaseAgenticTool):
    name = "media_tool"
    description = "Generate or edit images, videos, speech, sound effects and music using user-configured media APIs. Discover configured connections/models with capabilities; submit once, then query or cancel the durable job_id. Media generation may cost money. 图片、视频、语音、音效、音乐生成。"
    input_schema = MediaInput

    def is_read_only_call(self, action="submit", **kwargs):
        return action in {"capabilities", "list"}

    def is_concurrency_safe_call(self, action="submit", **kwargs):
        return action in {"capabilities", "list"}

    def check_permissions(self, permission_context):
        if permission_context.get("input_payload", {}).get("retry_unknown"):
            return {"behavior": "ask", "source": "uncertain_media_submission",
                "reason": "An earlier generation may already have been accepted and charged. Approve only after checking the existing provider task; another submission may incur another charge."}

    def prepare_input(self, values):
        values = dict(values)
        if values.get("action") == "submit":
            root = self.context.get("session_cwd") or os.getcwd()
            sandbox = self.context.get("sandbox_manager")
            resolve = sandbox.resolve_path if sandbox and sandbox.active_sandbox else lambda value: os.path.abspath(os.path.join(root, value))
            values["output_dir"] = resolve(values.get("output_dir") or "generated")
            values["image_paths"] = [resolve(path) for path in values.get("image_paths", [])]
            binding, key = config.resolve(values["connection"], values["model"])
            values["_target_revision"] = config.revision(binding, key)
        return values

    async def __call__(self, **values):
        # The engine has prepared the target before approval. Revalidating public
        # inputs must not silently resolve a changed target after the user waits.
        revision = values.pop("_target_revision", None)
        validated = self.input_schema(**values).model_dump()
        if revision is None:
            validated = self.prepare_input(validated)
            revision = validated.pop("_target_revision", None)
        return await self.execute(**validated, _target_revision=revision)

    def build_permission_summary(self, **values):
        if values.get("action") == "submit":
            binding, key = config.resolve(values["connection"], values["model"])
            if values.get("_target_revision") != config.revision(binding, key):
                raise ValueError("Media target changed before approval; no request was submitted")
            return "Generate media using " + binding["base_url"] + "/" + binding["model"]["model"] + "\nPrompt: " + str(values.get("prompt", ""))[:300] + "\nOutput: " + str(values.get("output_dir")) + "\nUpload images: " + ", ".join(values.get("image_paths", []))
        if values.get("job_id"):
            record = jobs.load(self.context, values["job_id"])
            return str(values["action"]) + " " + record["id"] + "\nProvider: " + record["binding"]["base_url"] + "\nOutput: " + record["output_dir"]
        return self.description

    def _save(self, record):
        record["updated_at"] = int(time.time() * 1000)
        try:
            jobs.save(self.context, record)
        except (OSError, ValueError) as error:
            raise PersistenceError("Could not save media request receipt; execution may have begun. Do not submit it again.") from error

    def _response(self, record, *, cancel=False):
        ok = record["phase"] not in {"unknown", "failed", "cancelled", "result_unavailable", "download_failed"} or (cancel and record["phase"] == "cancelled")
        value = jobs.public(record)
        value["finished"] = record["phase"] == "downloaded"
        if record["phase"] in {"submitted", "cancel_requested", "download_failed", "result_unavailable"}:
            value["next"] = {"action": "status", "job_id": record["id"]}
            value["notice"] = "This existing task has no confirmed saved outputs yet. Inspect this job_id; do not submit another generation."
        return build_tool_result(ok=ok, is_error=not ok, content=json.dumps(value, ensure_ascii=False),
            metadata={"job_id": record["id"], "phase": record["phase"], "output_files": record.get("files", []) if value["finished"] else []})

    async def execute(self, action, connection=None, model=None, prompt=None, inputs=None, image_paths=None,
                      output_dir=None, job_id=None, wait_ms=30000, retry_unknown=False, _target_revision=None):
        isolation = self.context.get("execution_sandbox")
        if isolation and isolation.enabled:
            isolation.check_tool(self.name)  # Fail closed even when called outside the normal wrapper.
        if action == "capabilities":
            return build_tool_result(ok=True, content=json.dumps({"connections": config.public_connections(),
                "protocols": ["fal-queue"], "max_wait_ms": 120000, "generation_requires_approval": True}, ensure_ascii=False))
        if action == "list":
            return build_tool_result(ok=True, content=json.dumps({"jobs": jobs.listing(self.context)}, ensure_ascii=False))
        record, key = None, ""
        try:
            if action == "submit":
                binding, key = config.resolve(connection, model)
                if _target_revision is not None and config.revision(binding, key) != _target_revision:
                    raise ValueError("Media target or key changed while waiting for approval; no request was submitted")
                payload = copy.deepcopy(binding["model"]["defaults"])
                payload.update(inputs or {})
                payload[binding["model"]["prompt_field"]] = prompt
                encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
                if len(encoded.encode("utf-8")) > 65536 or re.search(r'"(?:api[_-]?key|authorization|access[_-]?token|password|secret)"\s*:', encoded, re.I):
                    raise ValueError("Media inputs exceed 64 KB or contain credentials; use the connection API key")
                if payload.get("sync_mode"):
                    raise ValueError("Queue media requires URL outputs; sync_mode is not supported")
                images = []
                for path in image_paths or []:
                    image = Path(path)
                    if image.stat().st_size > 5 * 1024 * 1024:
                        raise ValueError("Each uploaded reference image is limited to 5 MB")
                    mime, _ = files.identify(image, "image")
                    images.append("data:" + mime + ";base64," + base64.b64encode(image.read_bytes()).decode())
                if binding["model"]["kind"] == "image-edit" and not images and not payload.get(binding["model"]["image_field"]):
                    raise ValueError("Image editing requires reference images")
                if images:
                    field = binding["model"]["image_field"]
                    if field in {"image_url", "first_frame_url"}:
                        if len(images) != 1:
                            raise ValueError("This configured model takes one reference image")
                        payload[field] = images[0]
                    else:
                        payload[field] = images
                digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                duplicate = jobs.uncertain_duplicate(self.context, binding, digest)
                if duplicate and not retry_unknown:
                    return self._response(duplicate)
                identity = "media-" + uuid.uuid4().hex
                from xuloom.context.tool_media import artifact_owner
                record = {"id": identity, "session_id": artifact_owner(self.context), "agent_id": self.context["agent_id"],
                    "connection_id": connection, "model_id": model, "kind": binding["model"]["kind"], "binding": binding,
                    "output_dir": os.path.abspath(output_dir or os.path.join(self.context["session_cwd"], "generated")),
                    "phase": "submitting", "created_at": int(time.time() * 1000), "files": [],
                    "input_digest": digest}
                self._save(record)  # Intent is durable before the one and only POST.
                value = await queue.request(binding, key, "POST", binding["base_url"] + "/" + binding["model"]["model"], payload)
                candidate = value.get("request_id")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate):
                    record["request_id"] = candidate
                record.update(queue.submit_receipt(binding, value), phase="submitted")
                self._save(record)
            else:
                record = jobs.load(self.context, job_id)
                if record["phase"] in {"downloaded", "failed", "cancelled"}:
                    return self._response(record, cancel=action == "cancel")
                binding, key = config.resolve(record["connection_id"], record["model_id"], frozen=record["binding"])
                if not all(record.get(field) for field in ("request_id", "status_url", "response_url", "cancel_url")):
                    record.update(phase="unknown", error="Submission outcome is unknown; no new POST was made. Inspect this task with the provider before any new generation.")
                    self._save(record); return self._response(record)
            if action == "cancel":
                value = await queue.request(binding, key, "PUT", record["cancel_url"])
                status = value.get("status")
                if status not in {"OK", "CANCELLATION_REQUESTED", "ALREADY_COMPLETED", "NOT_FOUND"}:
                    raise ValueError("Provider returned an unknown cancellation result")
                record["phase"] = "cancelled" if status == "OK" else "cancel_requested"
                record["provider_status"] = status
                record["error"] = "Cancellation was requested; remote stop has not been confirmed" if status != "OK" else "Provider confirmed removal from its queue"
                self._save(record); return self._response(record, cancel=True)
            deadline = time.monotonic() + wait_ms / 1000
            while True:
                status = await queue.request(binding, key, "GET", record["status_url"])
                if status.get("request_id", record["request_id"]) != record["request_id"]:
                    raise ValueError("Provider returned status for a different media request")
                state = status.get("status")
                if state not in {"IN_QUEUE", "IN_PROGRESS", "COMPLETED"}:
                    raise ValueError("Unknown provider media status")
                record["provider_status"] = state
                record.pop("error", None)
                if state == "COMPLETED":
                    if status.get("error") or status.get("error_type"):
                        record.update(phase="failed", error="Provider completed with an error; no generated output was accepted")
                        self._save(record); return self._response(record)
                    record["phase"] = "result_pending"
                    self._save(record)
                    result = await queue.request(binding, key, "GET", record["response_url"])
                    descriptors = queue.output_files(binding, result)
                    record["phase"] = "downloading"
                    self._save(record)
                    completed = []
                    for index, descriptor in enumerate(descriptors):
                        file = await files.download(binding, descriptor, record["output_dir"], record["id"] + "-" + str(index + 1))
                        completed.append(file)
                        record["files"] = completed
                        self._save(record)
                        from xuloom.features.file_changes import record_written_file
                        record_written_file(self.context, file["path"], None, "generate")
                    record.update(phase="downloaded", files=completed)
                    self._save(record); return self._response(record)
                record["phase"] = "cancel_requested" if record["phase"] == "cancel_requested" else "submitted"
                self._save(record)
                if time.monotonic() >= deadline:
                    return self._response(record)
                await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
        except PersistenceError:
            raise
        except asyncio.CancelledError:
            if record:
                if record["phase"] == "submitting":
                    record["phase"] = "unknown"
                record["error"] = "Local waiting stopped. Provider execution may continue; inspect or cancel this existing job_id."
                self._save(record)
            raise
        except Exception as error:
            message = _error(error)
            if key:
                message = message.replace(key, "[redacted]")
            if record:
                phase = record["phase"]
                if phase == "submitting":
                    rejected = isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {400, 401, 403, 404, 422, 429}
                    record["phase"] = "failed" if rejected else "unknown"
                elif phase in {"downloading", "download_failed"}:
                    record["phase"] = "download_failed"
                elif phase == "result_pending":
                    record["phase"] = "result_unavailable"
                record["error"] = message
                self._save(record)
                value = self._response(record)
                value.update(ok=False, is_error=True)
                return value
            return build_tool_result(ok=False, is_error=True, content=message)
