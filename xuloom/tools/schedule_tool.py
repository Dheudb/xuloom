import time
from typing import Literal, Optional
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result


class ScheduleInput(BaseModel):
    action: Literal["add", "watch", "list", "pause", "resume", "cancel"]
    prompt: str = ""
    delay_seconds: float = Field(default=1800, ge=0, le=31536000)
    interval_seconds: Optional[float] = Field(default=None, ge=60, le=31536000)
    schedule_id: str = ""
    worker_ids: list[str] = Field(default_factory=list)
    marker_file: str = ""
    request_id: str = Field(default="", max_length=128)


class ScheduleTool(BaseAgenticTool):
    name = "schedule_tool"
    description = "Schedule an authorized delayed message or watch all specified workers settle / a workspace completion marker appear, then follow up once. watch needs request_id and either worker_ids or marker_file. Waiting uses no model requests. Requires App or xuloom schedule serve; busy sessions queue."
    input_schema = ScheduleInput
    is_read_only = False

    def is_read_only_call(self, action, **kwargs): return action == "list"

    async def execute(self, action, prompt="", delay_seconds=1800, interval_seconds=None, schedule_id="", worker_ids=None, marker_file="", request_id=""):
        from xuloom.features import schedules
        callback = self.context.get("schedule_add")
        owner = self.context.get("session_id")
        if self.context.get("agent_depth", 0): raise ValueError("Only the conversation owner may schedule messages")
        if action in {"add", "watch"}:
            if not callback: raise ValueError("Scheduling is unavailable in this context")
            if action == "watch":
                if bool(worker_ids) == bool(marker_file): raise ValueError("Choose worker_ids or marker_file")
                trigger = {"kind": "workers", "agent_ids": worker_ids} if worker_ids else {"kind": "file", "path": marker_file}
                result = callback(prompt, due_at=time.time(), interval=interval_seconds, trigger=trigger, request_id=request_id)
            else: result = callback(prompt, due_at=time.time()+delay_seconds, interval=interval_seconds)
        else:
            rows = [row for row in schedules.list_items() if row["session_id"] == owner]
            if action == "list": result = rows
            else:
                if not any(row["id"] == schedule_id for row in rows): raise ValueError("Unknown schedule for this conversation")
                result = schedules.change(schedule_id, action)
        return build_tool_result(ok=True, content=__import__('json').dumps(result, ensure_ascii=False))
