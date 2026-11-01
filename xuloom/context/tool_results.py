import os
import uuid
import copy
from typing import Any, Dict
from xuloom.core.storage import artifact_directory


def native_tool_content(payload, *, errors=None, **serialization):
    """Preserve provider-native media blocks; archive oversized text through the existing path."""
    blocks = payload.get("content") if isinstance(payload, dict) else payload
    if not isinstance(blocks, list):
        return None
    result = []
    def text_block(text):
        rendered = serialize_tool_result(text, **serialization)
        if len(text) > serialization.get("spill_threshold_chars", 5000) and rendered.startswith("Status: error") and errors is not None:
            errors.append("content_archive_failed")
        return {"type": "text", "text": rendered}
    if isinstance(payload, dict):
        summary = {**payload, "content": ""}
        result.append({"type": "text", "text": serialize_tool_result(summary, **serialization)})
    for block in blocks:
        if not isinstance(block, dict):
            result.append(text_block(str(block)))
        elif block.get("type") == "text":
            result.append(text_block(block.get("text", "")))
        elif block.get("type") in {"image", "document"}:
            result.append(copy.deepcopy(block))
        else:
            result.append(text_block(str(block)))
    return result


def build_tool_result(
    *,
    ok: bool,
    content: str,
    metadata: Dict[str, Any] = None,
    is_error: bool = False,
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "content": content,
        "metadata": metadata or {},
        "is_error": is_error,
    }


def serialize_tool_result(
    payload: Any,
    *,
    spill_threshold_chars: int = 5000,
    artifact_dir: str = None,
    spill_recorder=None,
) -> str:
    artifact_dir = artifact_dir if artifact_dir is not None else str(artifact_directory())
    if isinstance(payload, str) and len(payload) > spill_threshold_chars:
        payload = build_tool_result(ok=True, content=payload)
    if isinstance(payload, dict) and {"ok", "content", "metadata", "is_error"}.issubset(payload.keys()):
        content = str(payload.get("content", ""))
        metadata = dict(payload.get("metadata", {}) or {})
        # Producers can mark links already present in their content. Other
        # tools may supply sources only as metadata; that evidence stays visible.
        if metadata.pop("sources_in_content", False) is True:
            metadata.pop("sources", None)
        is_error = bool(payload.get("is_error", False))
        ok = bool(payload.get("ok", False))

        if content and len(content) > spill_threshold_chars:
            try:
                os.makedirs(artifact_dir, exist_ok=True)
                file_path = os.path.abspath(
                    os.path.join(
                        artifact_dir,
                        f"tool-result-{uuid.uuid4().hex}.txt",
                    )
                )
                from xuloom.core.storage import atomic_text
                atomic_text(file_path, content)
                metadata["content_spilled"] = True
                metadata["content_spilled_path"] = file_path
                metadata["original_content_chars"] = len(content)
                metadata["inline_preview_chars"] = min(800, len(content))
                metadata["truncated"] = True
                if callable(spill_recorder):
                    try:
                        spill_recorder(
                            file_path=file_path,
                            original_content_chars=len(content),
                            metadata=metadata,
                        )
                    except Exception:
                        pass
                preview = content[:800]
                content = (
                    "Tool result was too large for inline context and has been written to disk.\n"
                    f"Reference path: {file_path}\n"
                    "Read the complete result with file_read_tool using offset and limit for lines, or char_offset and char_limit for long single lines; this preview is not the full result.\n"
                    f"Preview:\n{preview}"
                )
            except Exception as e:
                metadata["content_spill_error"] = str(e)
                metadata["truncated"] = False
                ok = False
                is_error = True
                content = "Full result could not be archived; original content follows without truncation.\n" + content

        lines = [f"Status: {'ok' if ok else 'error'}"]
        if metadata:
            lines.append("Metadata:")
            for key, value in metadata.items():
                lines.append(f"- {key}: {value}")
        if content:
            lines.append("Content:")
            lines.append(content)

        rendered = "\n".join(lines).strip()
        if is_error and not rendered.startswith("Status: error"):
            rendered = "Status: error\n" + rendered
        return rendered or ("Status: ok" if ok else "Status: error")

    return str(payload)
