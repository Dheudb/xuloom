import json
from xuloom.utils.shell import shell_guidance
from typing import List, Optional
from pathlib import Path
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool

MAX_WAIT_MS = 120000


class CommandInput(BaseModel):
    action: str = Field("start", pattern="^(start|poll|write|read|cancel|list|capabilities)$")
    argv: Optional[List[str]] = Field(None, description="Preferred: executable and arguments, without shell interpolation")
    command: Optional[str] = Field(None, description="Alternative shell command")
    cwd: Optional[str] = Field(None, description="Defaults to this conversation's working directory")
    command_id: Optional[str] = None
    stdin: Optional[str] = Field(None, max_length=1000000, description="Exact UTF-8 text; no shell quoting required")
    keep_stdin_open: bool = False
    eof: bool = False
    wait_ms: int = Field(1000, ge=0, le=MAX_WAIT_MS,
        description="Maximum time to wait for this response, in milliseconds (0–120000 / 120 seconds). Returns earlier when the command finishes; separate from timeout_ms.")
    timeout_ms: int = Field(120000, ge=1, le=600000)
    stream: str = Field("stdout", pattern="^(stdout|stderr)$")
    offset: int = Field(0, ge=0, description="Byte cursor returned by the previous read")
    max_bytes: int = Field(4000, ge=4, le=100000)


class CommandTool(BaseAgenticTool):
    workspace_paths = {"cwd": True}
    name = "command_tool"
    description = "Run owned commands with exact UTF-8 stdin, bounded polling and paged lossless stdout/stderr. Reuse command_id to write/read; pipe sessions, not PTYs."
    input_schema = CommandInput

    def is_read_only_call(self, action="start", **kwargs):
        return action in {"poll", "read", "list", "capabilities"}

    def is_concurrency_safe_call(self, action="start", **kwargs):
        return self.is_read_only_call(action=action)

    def prompt(self):
        return (shell_guidance() + "Use command_tool for structured control JSON and UTF-8 stdin instead of shell pipes, temporary wrappers or quoting large text. "
                "start with argv and optional stdin; stdin closes by default. For interactive pipe programs set keep_stdin_open=true, "
                "then write exact stdin and eof=true when finished. poll never kills a live command. "
                "wait_ms accepts 0..120000 milliseconds (up to 120 seconds) and returns early on completion; timeout_ms separately bounds total process runtime. "
                "read stdout/stderr separately using next_offset until eof=true; require status=completed and exit_code=0 before treating control JSON as a successful response. Never interpret partial JSON. "
                "capabilities declares actual support; tty-only programs are unsupported. Command IDs belong to the creating agent.")

    async def execute(self, action="start", argv=None, command=None, cwd=None, command_id=None, stdin=None,
                      keep_stdin_open=False, eof=False, wait_ms=1000, timeout_ms=120000,
                      stream="stdout", offset=0, max_bytes=4000):
        manager = self.context["command_sessions"]
        owner = self.context["agent_id"]
        if action == "capabilities":
            return json.dumps({"stdin": True, "interactive_pipes": True, "pty": False, "paged_output": True,
                               "max_wait_ms": MAX_WAIT_MS, "max_timeout_ms": 600000, "output_limit_bytes_per_stream": manager.OUTPUT_LIMIT})
        if action == "list":
            return json.dumps({"commands": [manager.snapshot(r) for r in manager.records.values() if r["owner"] == owner]}, ensure_ascii=False)
        if action == "start":
            directory = Path(cwd or self.context["session_cwd"]).resolve()
            sandbox = self.context.get("sandbox_manager")
            if sandbox and sandbox.active_sandbox:
                directory = Path(sandbox.resolve_working_directory(str(directory)))
            command_id = await manager.start(argv=argv, command=command, cwd=str(directory), owner=owner,
                stdin=stdin, keep_stdin_open=keep_stdin_open, timeout_ms=timeout_ms)
        elif action == "write":
            await manager.write(command_id, owner, stdin or "", eof=eof)
        elif action == "cancel":
            return json.dumps(await manager.cancel(command_id, owner), ensure_ascii=False)
        elif action not in {"poll", "read"}:
            raise ValueError("Unknown command action")
        snapshot = await manager.poll(command_id, owner, 0 if action == "read" else wait_ms)
        completion = self.context.get("workflow_completion")
        if completion and completion.enabled and snapshot.get("status") == "completed" and snapshot.get("exit_code") == 0:
            control = manager.read(command_id, owner, "stdout", 0, 1000000)
            if control.get("eof") and not control.get("has_more"):
                completion.observe(command_id, control["text"])
        snapshot["output"] = manager.read(command_id, owner, stream, offset, max_bytes)
        return json.dumps(snapshot, ensure_ascii=False)
