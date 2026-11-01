"""Terminal presentation over SSHBridge; all execution stays in the existing runtime."""
import asyncio
import json
import os
import stat
import sys
import shlex
import uuid

from rich.console import Console
from rich.markdown import Markdown

from xuloom.remote.proxy import SSHBridge
from xuloom.remote.ssh import RemoteDisconnected, RemoteRequestError


class PipeInput:
    """Cancellable UTF-8 line input without a blocked executor thread on exit."""
    def __init__(self, stream):
        self.fd, self.buffer, self.eof = stream.fileno(), b"", False
        self.regular = stat.S_ISREG(os.fstat(self.fd).st_mode)
        if os.name == "nt" and not self.regular:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            self.ctypes, self.count = ctypes, wintypes.DWORD()
            self.handle = msvcrt.get_osfhandle(self.fd)
            self.peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            self.peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            self.peek.restype = wintypes.BOOL

    async def read(self):
        while b"\n" not in self.buffer and not self.eof:
            size = 65536
            if not self.regular:
                if os.name == "nt":
                    if not self.peek(self.handle, None, 0, None, self.ctypes.byref(self.count), None):
                        error = self.ctypes.get_last_error()
                        if error in {6, 109, 232}: self.eof = True; break
                        raise self.ctypes.WinError(error)
                    size = min(size, self.count.value)
                else:
                    import select
                    if not select.select([self.fd], [], [], 0)[0]: size = 0
            if not size:
                await asyncio.sleep(.05); continue
            chunk = os.read(self.fd, size)
            if not chunk: self.eof = True
            self.buffer += chunk
            if len(self.buffer) > 1_000_000: raise ValueError("Terminal input exceeds 1 MB")
        if not self.buffer: raise EOFError
        line, separator, rest = self.buffer.partition(b"\n")
        self.buffer = rest if separator else b""
        return line.rstrip(b"\r").decode("utf-8")


async def interactive_chat(identity, *, session_id=None, binding=None, machine=None, permission="ask", collaboration=None,
                           execution=None, read=None, console=None, on_event=None):
    console = console or Console()
    queue, lost = asyncio.Queue(), asyncio.Event()
    def event(value):
        if on_event: on_event(value)
        if value["type"] == "engine_exit": lost.set()
        queue.put_nowait(value)
    bridge = SSHBridge(identity, binding, machine, event)
    capabilities = {"skills": []}
    attachment_ids = []
    class Skills:
        def active(self): return [item for item in capabilities["skills"] if item.get("valid", True) and item.get("enabled", True) and not item.get("shadowed")]
    if read is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from xuloom.skills.terminal import input_session
            terminal = input_session(lambda: Skills())
            read = terminal.prompt_async
        else:
            lines = PipeInput(sys.stdin)
            async def read(label):
                console.print(label, end="", markup=False)
                return await lines.read()

    async def ask(label):
        entry = asyncio.create_task(read(label)); disconnected = asyncio.create_task(lost.wait())
        try:
            await asyncio.wait({entry, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if lost.is_set(): raise RemoteDisconnected("SSH connection lost; remote outcome is unconfirmed. Reopen the saved chat to inspect it; no task was resent.")
            return await entry
        finally:
            for task in (entry, disconnected):
                if not task.done(): task.cancel()
            await asyncio.gather(entry, disconnected, return_exceptions=True)

    async def interaction(value):
        if value["type"] == "questions":
            answer = {}
            for question in value["questions"]:
                console.print(question["question"], markup=False)
                options = question.get("options", [])
                for index, option in enumerate(options, 1):
                    console.print(f"{index}. {option['label']}  {option.get('description', '')}", markup=False)
                console.print("输入序号（多选用逗号分隔），或直接输入自己的回答。", markup=False)
                while True:
                    raw = (await ask("选择：")).strip()
                    if not raw: continue
                    if all(part.strip().isdigit() for part in raw.split(",")):
                        indices = [int(part.strip()) - 1 for part in raw.split(",")]
                        if not all(0 <= index < len(options) for index in indices) or not question.get("multiSelect") and len(indices) != 1:
                            console.print("请选择有效序号。", markup=False); continue
                        labels = list(dict.fromkeys(options[index]["label"] for index in indices))
                        answer[question["question"]] = labels if question.get("multiSelect") else labels[0]
                    else: answer[question["question"]] = raw
                    break
        else:
            request = value.get("request", {})
            console.print(value.get("content") or request.get("summary") or request.get("tool_name", "Approval required"), markup=False)
            choices = {"1": "allow", "2": "deny"}
            label = "1 允许一次 / 2 拒绝"
            if value["type"] == "permission": choices["3"] = "always"; label += " / 3 本会话允许此工具"
            while True:
                raw = (await ask(label + "：")).strip()
                if raw in choices: answer = choices[raw]; break
                console.print("请输入有效序号；空输入不会自动批准。", markup=False)
        await bridge.dispatch("respond", {"requestId": value["requestId"], "answer": answer})

    active_turn = False

    async def stop():
        nonlocal active_turn
        await asyncio.shield(bridge.dispatch("stop", {}))
        # The stop acknowledgement follows the old turn's final events.
        while not queue.empty(): queue.get_nowait()
        active_turn = False

    async def turn(method, payload):
        nonlocal active_turn, attachment_ids
        await bridge.dispatch(method, payload)
        if method == "prompt": attachment_ids = []
        active_turn = True
        receipt, streamed = None, ""
        while True:
            value = await queue.get(); kind = value["type"]
            if kind == "engine_exit": raise RemoteDisconnected(value.get("message", "SSH disconnected; outcome unknown"))
            if kind in {"questions", "permission", "plan_approval"}: await interaction(value)
            elif kind == "text_delta":
                text = value.get("text", ""); streamed += text; console.print(text, end="", markup=False)
            elif kind in {"tool_start", "tool_scheduled"}: console.print("\n· " + value.get("tool_name", "tool"), markup=False)
            elif kind == "final":
                receipt = value.get("result")
                text = value.get("text", "")
                if text and text != streamed: console.print(Markdown(text))
            elif kind == "turn_error": receipt = value.get("result") or {"ok": False, "reason": "error", "text": value.get("message", "")}
            elif kind == "turn_cancelled": receipt = receipt or {"ok": False, "reason": "cancelled"}
            elif kind == "turn_finished":
                active_turn = False
                console.print()
                if not receipt or not receipt.get("ok"):
                    console.print((receipt or {}).get("text") or "Execution ended: " + (receipt or {}).get("reason", "missing_result"), markup=False)
                return

    result = {"ok": True, "reason": "closed"}
    try:
        connection = await bridge.dispatch("remote.connection", {})
        if not session_id: session_id = (await bridge.dispatch("sessions.create", {}))["sessionId"]
        initial = await bridge.dispatch("initialize", {"sessionId": session_id, "permissionMode": permission})
        capabilities = initial["capabilities"]
        if collaboration is not None: await bridge.dispatch("collaboration.set", {"mode": collaboration})
        if execution is not None: await bridge.dispatch("execution.set", {"mode": execution})
        console.print(f"SSH · {connection['profile']['host']} · {connection['machine']['hostname']}\n{initial['project']}\nSession: {session_id}", markup=False)
        for message in initial["history"]:
            console.print(message["role"] + ":", markup=False); console.print(Markdown(message.get("text", "")))
        console.print("/help 查看命令；$ 选择远端 Skill；Ctrl+C 停止当前回合。", markup=False)
        plan = False
        while True:
            try:
                text = (await ask("❯ ")).strip()
                if not text: continue
                command, _, argument = text.partition(" ")
                if command in {"/quit", "/q", "/exit"}: break
                if command == "/help":
                    console.print("/link /status /git /summary [all] /files [path] /read <path> /preview <HTML path> /skills /agents /model list|status|use N\n/attach <local file> /attachments /attachments use UUID /attachments clear /download UUID <local file>\n/permission ask|full /collaboration solo|auto|ultra /thinking enabled|disabled /effort LEVEL\n/sandbox host|workspace /plan on|off /continue /quit", markup=False); continue
                if command == "/attach":
                    filename = argument.strip().strip('"')
                    if not filename: raise ValueError("Use /attach <local file>")
                    identities = [str(uuid.uuid4())]
                    console.print("Attachment: " + identities[0], markup=False)
                    items = await bridge.dispatch("attachments.import", {"files": [{"path": filename}], "ids": identities, "existing": attachment_ids})
                    attachment_ids = [item["id"] for item in items]
                    console.print(json.dumps(items, ensure_ascii=False, indent=2), markup=False); continue
                if command == "/attachments":
                    if argument == "clear": attachment_ids = []
                    elif argument.startswith("use "):
                        identity = argument[4:].strip()
                        items = (await bridge.dispatch("attachments.list", {"ids": [identity]}))["items"]
                        if not items or not items[0].get("available"): raise ValueError("Attachment is unavailable; re-upload the original file")
                        if len(attachment_ids) >= 10: raise ValueError("Each message supports at most 10 attachments")
                        attachment_ids = list(dict.fromkeys([*attachment_ids, identity]))
                    elif argument: raise ValueError("Use /attachments, /attachments use UUID or /attachments clear")
                    else:
                        value = await bridge.dispatch("attachments.list", {})
                        console.print(json.dumps(value, ensure_ascii=False, indent=2), markup=False)
                    console.print("Next message attachments: " + ", ".join(attachment_ids), markup=False); continue
                if command == "/download":
                    parts = shlex.split(argument, posix=False)
                    if len(parts) != 2: raise ValueError('Use /download UUID "local filename"')
                    value = await bridge.dispatch("attachments.download", {"id": parts[0], "path": parts[1].strip('"')})
                    console.print(json.dumps(value, ensure_ascii=False, indent=2), markup=False); continue
                if command == "/link":
                    from xuloom.features.chat_links import remote_chat_link
                    console.print(remote_chat_link(connection["profile"], session_id), markup=False, soft_wrap=True); continue
                if command == "/plan":
                    if argument not in {"on", "off"}: raise ValueError("Use /plan on|off")
                    plan = argument == "on"; continue
                methods = {"/summary": ("session.summary", {"all": argument == "all"}), "/status": ("session.status", {"sessionId": session_id}), "/git": ("git.status", {}),
                    "/files": ("files.list", {"path": argument}), "/read": ("files.read", {"path": argument}),
                    "/preview": ("files.htmlInfo", {"path": argument}),
                    "/agents": ("agents.list", {}), "/skills": ("capabilities.list", {}),
                    "/permission": ("permissions.set", {"mode": argument}), "/collaboration": ("collaboration.set", {"mode": argument}),
                    "/sandbox": ("execution.set", {"mode": argument})}
                if command in methods:
                    value = await bridge.dispatch(*methods[command])
                    if command == "/skills": capabilities = value
                    console.print(json.dumps(value, ensure_ascii=False, indent=2), markup=False); continue
                if command in {"/thinking", "/effort"}:
                    cfg = await bridge.dispatch("config.get", {})
                    if command == "/thinking" and argument not in {"enabled", "disabled"}: raise ValueError("Use /thinking enabled|disabled")
                    await bridge.dispatch("config.reasoning", {"connectionId": cfg["connectionId"], "model": cfg["model"],
                        "thinking" if command == "/thinking" else "effort": argument})
                    continue
                if command == "/model":
                    cfg = await bridge.dispatch("config.get", {})
                    choices = [(item, model) for item in cfg["connections"] for model in item["models"]]
                    if argument == "status": console.print(json.dumps(cfg, ensure_ascii=False, indent=2), markup=False)
                    elif argument == "list":
                        for index, (item, model) in enumerate(choices, 1): console.print(f"{index}. {item['connectionName']} · {model}", markup=False)
                    elif argument.startswith("use ") and argument[4:].isdigit() and 1 <= int(argument[4:]) <= len(choices):
                        item, model = choices[int(argument[4:]) - 1]
                        await bridge.dispatch("config.select", {"connectionId": item["connectionId"], "model": model})
                        console.print("Model: " + model, markup=False)
                    else: raise ValueError("Use /model list or /model use N")
                    continue
                if command.startswith("/") and command != "/continue": raise ValueError("Unknown command; use /help")
                if command == "/continue" and argument: raise ValueError("/continue cannot replace the saved prompt")
                await turn("prompt.resume" if command == "/continue" else "prompt", {} if command == "/continue" else {"text": text, "mode": "plan" if plan else "normal", "attachments": attachment_ids})
            except (KeyboardInterrupt, asyncio.CancelledError):
                await stop()
                console.print("已停止当前连接拥有的任务；/continue 继续。", markup=False)
            except RemoteDisconnected: raise
            except (RemoteRequestError, ValueError) as error:
                if active_turn: await stop()
                console.print(str(error), markup=False)
            except EOFError: break
    except RemoteDisconnected as error:
        console.print(str(error), markup=False); result = {"ok": False, "reason": "remote_disconnected", "outcome_unknown": True}
    finally:
        try: await bridge.close()
        except (ConnectionError, OSError, ValueError) as error:
            console.print(str(error), markup=False); result = {"ok": False, "reason": "remote_disconnected", "outcome_unknown": True}
    return {**result, "session_id": session_id}
