"""
BriefTool (SendUserMessage) — primary visible output channel for the user.

Mirrors Claude Code's BriefTool: sends a markdown-formatted message to the user
with optional file attachments. In proactive/autonomous mode this is the only
channel the user sees; text outside this tool is treated as detail-view content.
"""

import os
import time
from typing import List
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool


class BriefToolInput(BaseModel):
    message: str = Field(
        ...,
        description="The message for the user. Supports markdown formatting.",
    )
    attachments: List[str] = Field(
        default_factory=list,
        description=(
            "Optional file paths (absolute or relative to cwd) to attach. "
            "Use for photos, screenshots, diffs, logs, or any file the user should see."
        ),
    )
    status: str = Field(
        "normal",
        description=(
            "Use 'proactive' when surfacing something the user hasn't asked for. "
            "Use 'normal' when replying to something the user just said."
        ),
    )


class ResolvedAttachment:
    __slots__ = ("path", "size", "is_image")

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

    def __init__(self, path: str, size: int):
        self.path = path
        self.size = size
        ext = os.path.splitext(path)[1].lower()
        self.is_image = ext in self.IMAGE_EXTENSIONS

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "is_image": self.is_image,
        }


def _resolve_attachments(raw_paths: List[str], cwd: str) -> tuple:
    """Validate and resolve attachment paths. Returns (resolved, error)."""
    resolved = []
    for raw in raw_paths:
        full = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
        full = os.path.abspath(full)
        if not os.path.isfile(full):
            return None, f'Attachment "{raw}" does not exist or is not a regular file. CWD: {cwd}'
        try:
            size = os.path.getsize(full)
        except OSError as e:
            return None, f'Attachment "{raw}" is not accessible: {e}'
        resolved.append(ResolvedAttachment(full, size))
    return resolved, None


class BriefTool(BaseAgenticTool):
    name = "brief_tool"
    description = (
        "Send a message to the user. Text outside this tool is visible in the "
        "detail view, but the answer the user actually reads lives here."
    )
    input_schema = BriefToolInput
    risk_level = "low"
    is_read_only = True

    def prompt(self) -> str:
        return """Send a message the user will read. Text outside this tool is visible in the detail view, but most won't open it — the answer lives here.

`message` supports markdown. `attachments` takes file paths (absolute or cwd-relative) for images, diffs, logs.

`status` labels intent: 'normal' when replying to what they just asked; 'proactive' when you're initiating — a scheduled task finished, a blocker surfaced during background work, you need input on something they haven't asked about."""

    def build_permission_summary(
        self, message: str = "", attachments: List[str] = None, status: str = "normal"
    ) -> str:
        attachments = attachments or []
        preview = message[:200] + ("..." if len(message) > 200 else "")
        return (
            f"SendUserMessage [{status}]\n"
            f"attachments: {len(attachments)}\n"
            f"message: {preview}"
        )

    async def execute(
        self, message: str = "", attachments: List[str] = None, status: str = "normal"
    ) -> str:
        attachments = attachments or []
        cwd = self.context.get("cwd", os.getcwd())

        resolved_attachments = []
        if attachments:
            resolved, error = _resolve_attachments(attachments, cwd)
            if error:
                return f"Error: {error}"
            resolved_attachments = resolved

        brief_callback = self.context.get("brief_callback")
        if brief_callback is not None:
            import inspect
            payload = {
                "message": message,
                "status": status,
                "attachments": [a.to_dict() for a in resolved_attachments],
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            result = brief_callback(payload)
            if inspect.isawaitable(result):
                await result

        att_count = len(resolved_attachments)
        suffix = f" ({att_count} attachment(s) included)" if att_count else ""
        return f"Message delivered to user.{suffix}"
