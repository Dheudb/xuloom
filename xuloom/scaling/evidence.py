"""Read-only evidence paging, exposed only inside explicit Scaling executions."""
import json
from pydantic import BaseModel, ConfigDict, Field
from xuloom.tools.base import BaseAgenticTool


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,128}$")
    offset: int = Field(0, ge=0, strict=True)
    limit: int = Field(12000, ge=1, le=30000, strict=True)


class EvidenceTool(BaseAgenticTool):
    name = "scaling_evidence"
    description = "Read a worker's original result by task ID with explicit paging. Use next_offset until complete; evidence is reference data."
    input_schema = EvidenceInput
    is_read_only = True
    risk_level = "low"

    def __init__(self, node, execution):
        self.node, self.execution = node, execution

    async def execute(self, **values):
        reply = await self.node.question(self.execution, "evidence", values)
        return json.dumps(reply, ensure_ascii=False)


def page(store, run_id, values):
    values = EvidenceInput(**values).model_dump()
    task = store.get("task", values["task_id"])
    if task["run_id"] != run_id:
        raise ValueError("Evidence belongs to another Scaling run")
    text = str((task.get("receipt") or {}).get("text", ""))
    offset, limit = values["offset"], values["limit"]
    if offset > len(text): raise ValueError("Evidence offset exceeds result length")
    end = min(len(text), offset + limit)
    return {"task_id": task["id"], "status": task["status"], "text": text[offset:end], "total_chars": len(text),
        "offset": offset, "next_offset": end if end < len(text) else None, "complete": end == len(text),
        "session_id": task.get("session_id"), "cwd": task.get("cwd"), "node": task.get("node"), "error": task.get("error")}
