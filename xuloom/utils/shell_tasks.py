import asyncio
import os
import time
import uuid
from xuloom.utils.shell import decode_output, process_options, terminate_process_tree
from typing import Optional


class ShellTaskManager:
    """
    Manages long-running shell tasks with file-backed logs.
    """

    def __init__(self, log_dir=".xuloom/shell-tasks"):
        self.log_dir = log_dir
        self.tasks = {}

    async def start_task(self, command: str, cwd: str = None, owner: str = None) -> dict:
        os.makedirs(self.log_dir, exist_ok=True)
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        log_path = os.path.abspath(os.path.join(self.log_dir, f"{task_id}.log"))
        normalized_cwd = os.path.abspath(cwd or os.getcwd())

        log_handle = open(log_path, "ab")
        try:
            process = await asyncio.create_subprocess_shell(command, cwd=normalized_cwd,
                stdout=log_handle, stderr=log_handle, **process_options())
        except BaseException:
            log_handle.close()
            raise

        self.tasks[task_id] = {
            "owner": owner,
            "task_id": task_id,
            "command": command,
            "cwd": normalized_cwd,
            "log_path": log_path,
            "started_at": time.time(),
            "status": "running",
            "pid": process.pid,
            "exit_code": None,
            "process": process,
            "log_handle": log_handle,
        }
        return self._export_record(task_id)

    def get_task(self, task_id: str, owner=None) -> Optional[dict]:
        if task_id not in self.tasks or owner is not None and self.tasks[task_id].get("owner") != owner:
            return None
        self._refresh_task(task_id)
        return self._export_record(task_id)

    def list_tasks(self, owner=None) -> list[dict]:
        for task_id in list(self.tasks.keys()):
            self._refresh_task(task_id)
        return [self._export_record(task_id) for task_id, row in self.tasks.items() if owner is None or row.get("owner") == owner]

    def read_output(self, task_id: str, tail_chars: int = 4000, owner=None) -> Optional[dict]:
        record = self.get_task(task_id, owner)
        if not record:
            return None

        try:
            with open(record["log_path"], "rb") as f:
                content = decode_output(f.read())
        except FileNotFoundError:
            content = ""

        if tail_chars and len(content) > tail_chars:
            content = content[-tail_chars:]

        record["output"] = content
        record["output_chars"] = len(content)
        return record

    async def terminate_task(self, task_id: str, owner=None) -> Optional[dict]:
        if task_id not in self.tasks or owner is not None and self.tasks[task_id].get("owner") != owner:
            return None

        record = self.tasks[task_id]
        process = record.get("process")
        if process and process.returncode is None:
            await terminate_process_tree(process)
        self._refresh_task(task_id)
        return self._export_record(task_id)

    def export_state(self, owner=None) -> list[dict]:
        return self.list_tasks(owner)

    async def close(self, owner=None):
        results = await asyncio.gather(*(self.terminate_task(identity) for identity, row in list(self.tasks.items())
            if owner is None or row.get("owner") == owner), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def load_state(self, payload: list):
        self.tasks = {}
        for item in payload or []:
            task_id = item.get("task_id")
            if not task_id:
                continue
            self.tasks[task_id] = {
                "owner": item.get("owner"),
                "task_id": task_id,
                "command": item.get("command", ""),
                "cwd": item.get("cwd", os.getcwd()),
                "log_path": item.get("log_path", ""),
                "started_at": item.get("started_at", time.time()),
                "status": item.get("status", "unknown"),
                "pid": item.get("pid"),
                "exit_code": item.get("exit_code"),
                "process": None,
                "log_handle": None,
            }

    def _refresh_task(self, task_id: str):
        record = self.tasks[task_id]
        process = record.get("process")
        if process is None:
            if record["status"] == "running":
                record["status"] = "detached"
            return

        if process.returncode is None:
            record["status"] = "running"
            return

        record["status"] = "completed" if process.returncode == 0 else "failed"
        record["exit_code"] = process.returncode
        log_handle = record.get("log_handle")
        if log_handle and not log_handle.closed:
            log_handle.close()
        record["log_handle"] = None

    def _export_record(self, task_id: str) -> dict:
        record = self.tasks[task_id]
        self._refresh_task(task_id)
        return {
            "owner": record.get("owner"),
            "task_id": record["task_id"],
            "command": record["command"],
            "cwd": record["cwd"],
            "log_path": record["log_path"],
            "started_at": record["started_at"],
            "status": record["status"],
            "pid": record["pid"],
            "exit_code": record["exit_code"],
        }
