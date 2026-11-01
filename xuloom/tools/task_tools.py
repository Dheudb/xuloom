import os
from typing import Optional

from pydantic import BaseModel, Field

from xuloom.context.tool_results import build_tool_result
from xuloom.tools.base import BaseAgenticTool


class TaskCreateToolInput(BaseModel):
    command: str = Field(..., description="Shell command to execute in the background.")
    cwd: Optional[str] = Field(None, description="Optional working directory for the background task.")


class TaskStatusToolInput(BaseModel):
    task_id: str = Field(..., description="Background task identifier.")


class TaskReadToolInput(BaseModel):
    task_id: str = Field(..., description="Background task identifier.")
    tail_chars: int = Field(4000, description="How many trailing characters of task output to return.")


class TaskKillToolInput(BaseModel):
    task_id: str = Field(..., description="Background task identifier.")


class TaskListToolInput(BaseModel):
    pass


class _BaseTaskTool(BaseAgenticTool):
    def _task_manager(self):
        return self.context.get("shell_task_manager")

    def _owner(self):
        return self.context.get("agent_id") if self.context.get("agent_depth") else None


class TaskCreateTool(_BaseTaskTool):
    workspace_paths = {"cwd": True}
    name = "task_create_tool"
    description = "Starts a long-running shell command in the background and returns a task id for later monitoring."
    input_schema = TaskCreateToolInput
    risk_level = "high"

    def prompt(self) -> str:
        return (
            "Use `task_create_tool` when a command should keep running beyond the current turn, "
            "such as dev servers, watchers, or long tests."
        )

    def build_permission_summary(self, command: str, cwd: str = None) -> str:
        return (
            "Background task creation requested.\n"
            f"cwd: {cwd or os.getcwd()}\n"
            f"command: {command}"
        )

    async def execute(self, command: str, cwd: str = None) -> dict:
        task_manager = self._task_manager()
        if not task_manager:
            return build_tool_result(
                ok=False,
                content="Shell task manager is unavailable.",
                metadata={"command": command, "cwd": cwd},
                is_error=True,
            )
        record = await task_manager.start_task(command, cwd=cwd, owner=self.context.get("agent_id"))
        return build_tool_result(
            ok=True,
            content=f"Background task started: {record['task_id']}",
            metadata=record,
        )


class TaskStatusTool(_BaseTaskTool):
    name = "task_status_tool"
    description = "Gets the latest status metadata for a background task."
    input_schema = TaskStatusToolInput
    is_read_only = True
    risk_level = "low"

    async def execute(self, task_id: str) -> dict:
        task_manager = self._task_manager()
        record = task_manager.get_task(task_id, owner=self._owner()) if task_manager else None
        if not record:
            return build_tool_result(
                ok=False,
                content=f"Unknown task '{task_id}'.",
                metadata={"task_id": task_id},
                is_error=True,
            )
        return build_tool_result(
            ok=True,
            content=f"Task '{task_id}' status is {record['status']}.",
            metadata=record,
        )


class TaskReadTool(_BaseTaskTool):
    name = "task_read_tool"
    description = "Reads the latest output log from a background task."
    input_schema = TaskReadToolInput
    is_read_only = True
    risk_level = "low"

    async def execute(self, task_id: str, tail_chars: int = 4000) -> dict:
        task_manager = self._task_manager()
        record = task_manager.read_output(task_id, tail_chars=tail_chars, owner=self._owner()) if task_manager else None
        if not record:
            return build_tool_result(
                ok=False,
                content=f"Unknown task '{task_id}'.",
                metadata={"task_id": task_id, "tail_chars": tail_chars},
                is_error=True,
            )
        output = record.pop("output", "")
        output_chars = record.pop("output_chars", 0)
        return build_tool_result(
            ok=True,
            content=output or "Task log is currently empty.",
            metadata={"tail_chars": tail_chars, "output_chars": output_chars, **record},
        )


class TaskKillTool(_BaseTaskTool):
    name = "task_kill_tool"
    description = "Stops a running background task."
    input_schema = TaskKillToolInput
    risk_level = "medium"

    def build_permission_summary(self, task_id: str) -> str:
        return f"Background task termination requested.\ntask_id: {task_id}"

    async def execute(self, task_id: str) -> dict:
        task_manager = self._task_manager()
        record = await task_manager.terminate_task(task_id, owner=self._owner()) if task_manager else None
        if not record:
            return build_tool_result(
                ok=False,
                content=f"Unknown task '{task_id}'.",
                metadata={"task_id": task_id},
                is_error=True,
            )
        return build_tool_result(
            ok=True,
            content=f"Task '{task_id}' terminated.",
            metadata=record,
        )


class TaskListTool(_BaseTaskTool):
    name = "task_list_tool"
    description = "Lists all known background tasks and their current statuses."
    input_schema = TaskListToolInput
    is_read_only = True
    risk_level = "low"

    async def execute(self) -> dict:
        task_manager = self._task_manager()
        tasks = task_manager.list_tasks(owner=self._owner()) if task_manager else []
        return build_tool_result(
            ok=True,
            content="Background task list retrieved.",
            metadata={"tasks": tasks, "task_count": len(tasks)},
        )
