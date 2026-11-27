"""Local terminal controls. No model requests or additional system instructions."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import uuid


def paths(text):
    """Accept quoted Windows paths without treating backslashes as shell escapes."""
    return [word[1:-1] if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'" else word
            for word in shlex.split(text, posix=False)]


def require_idle(engine):
    tasks = engine.tool_context.get("subagent_tasks")
    commands = engine.tool_context.get("command_sessions")
    shell = getattr(engine, "shell_task_manager", None)
    if (engine.has_active_turn() or tasks and any(not task.done() for task in tasks.tasks.values())
            or commands and any(row.get("status") == "running" for row in commands.records.values())
            or shell and any(row.get("status") == "running" for row in shell.list_tasks())):
        raise ValueError("Wait for running tasks or stop them before changing this session / 请先停止或等待当前任务")


def restore_permissions(engine):
    """The desktop's existing session index is also the CLI's permission preference."""
    from xuloom.core.storage import history_root
    path = history_root(engine.session_cwd) / "index.json"
    engine.permission_manager.approval_policy.set_mode("ask")
    engine.permission_manager.always_allow_tools.clear()
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))["sessions"].get(engine.session_id, {}).get("permissionMode", "ask")
        engine.permission_manager.approval_policy.set_mode(value)
        if value == "ask":
            engine.permission_manager.always_allow_tools.clear()


async def session_setting(engine, name, value):
    require_idle(engine)
    if name == "mode":
        engine.set_collaboration_mode(value)
        return
    engine.require_writer()
    if name == "permissions":
        policy = engine.permission_manager.approval_policy
        previous, allowed = policy.mode, set(engine.permission_manager.always_allow_tools)
        policy.set_mode(value)
        if value == "ask":
            engine.permission_manager.always_allow_tools.clear()
        try:
            if not engine.persist_session_state():
                raise OSError("Permission settings could not be saved / 权限设置保存失败")
            from xuloom.core.session_mutation import mutate
            mutate({"action": "metadata", "project": engine.session_cwd, "session_id": engine.session_id,
                    "patch": {"permissionMode": value}})
        except (OSError, ValueError, RuntimeError):
            policy.set_mode(previous)
            engine.permission_manager.always_allow_tools.update(allowed)
            raise
    elif name == "sandbox":
        from xuloom.security.execution import ExecutionSandbox
        candidate = ExecutionSandbox(engine.session_cwd, value)
        if candidate.enabled and not candidate.status()["available"]:
            raise ValueError(candidate.status()["message"])
        if engine.sandbox_manager.active_sandbox:
            raise ValueError("Exit the workspace copy before changing execution isolation / 请先退出工作区副本隔离")
        previous = engine.execution_sandbox.mode
        engine.execution_sandbox.mode = value
        if not engine.persist_session_state():
            engine.execution_sandbox.mode = previous
            raise OSError("Execution settings could not be saved / 隔离设置保存失败")
        # Shut down unconfined protocols before another turn. The existing sandbox
        # gate also refuses their tools; it never falls back to host execution.
        bridge = getattr(engine, "mcp_bridge", None)
        if bridge:
            await bridge.cleanup()
            engine.mcp_bridge = None
        lsp = getattr(engine, "lsp_manager", None)
        if lsp:
            for server in lsp.servers.values():
                await server.stop()
        for registry in (engine.available_tools, engine.latent_tools):
            for tool_name, tool in list(registry.items()):
                if hasattr(tool, "mcp_tool_name") or tool_name == "lsp_tool":
                    registry.pop(tool_name, None)
                    engine.activated_tools.discard(tool_name)
        if not candidate.enabled:
            await engine.start_protocols(lambda _: None)
    else:
        raise ValueError("Unknown session setting")


async def settings_command(engine, text, tui, console):
    name, _, argument = text.strip().partition(" ")
    name = name[1:].lower()
    choices = {"mode": ("solo", "auto", "ultra"), "permissions": ("ask", "full"), "sandbox": ("host", "workspace")}
    argument = argument.strip().lower()
    if argument == "status":
        console.print({"mode": engine.collaboration_mode, "permissions": engine.permission_manager.approval_policy.mode,
                       "sandbox": engine.execution_sandbox.mode}[name], markup=False)
        return
    if not argument:
        argument = tui.choose_menu(title="Session / 会话设置: " + name,
            body_lines=["Applies to this conversation; no model request / 仅修改当前会话，不调用模型"],
            options=[{"value": value, "label": value} for value in choices[name]] + [{"value": "", "label": "Cancel / 取消"}],
            prompt_label=name, default_value="")
    if not argument:
        return
    if argument not in choices[name]:
        raise ValueError("Use /" + name + " " + "|".join(choices[name]) + " or status")
    await session_setting(engine, name, argument)
    console.print(name + " → " + argument, markup=False)


class AttachmentDraft:
    """Explicitly selected next-message files, published using desktop's receipt format."""
    def __init__(self):
        self.items = []

    def add(self, engine, filenames):
        from xuloom.desktop import attachment_transfer as transfer
        if not filenames or len(self.items) + len(filenames) > 10:
            raise ValueError("Select 1–10 attachments / 每条消息最多 10 个附件")
        require_idle(engine)
        selected = []
        for filename in filenames:
            file = Path(os.path.abspath(os.path.join(engine.session_cwd, os.path.expanduser(filename))))
            if file.is_symlink() or not file.is_file():
                raise ValueError("Attachment is missing or linked: " + str(file))
            limit = 20 if file.suffix.lower() in transfer.IMAGE_EXTENSIONS else 64
            if file.stat().st_size > limit * 1024 * 1024:
                raise ValueError("Attachment exceeds size limit: " + str(file))
            selected.append(file)
        engine.require_writer()
        if not engine.persist_session_state():
            raise OSError("Session could not be saved; no attachments were added")
        staged = []
        try:
            for file in selected:
                upload = transfer.begin(engine.session_cwd, engine.session_id, str(uuid.uuid4()), file.name, file.stat().st_size)
                try:
                    digest, offset = hashlib.sha256(), 0
                    with file.open("rb") as stream:
                        for data in iter(lambda: stream.read(transfer.CHUNK), b""):
                            digest.update(data)
                            transfer.append(upload, offset, base64.b64encode(data).decode("ascii"))
                            offset += len(data)
                    staged.append(transfer.commit(upload, digest.hexdigest()))
                except BaseException:
                    transfer.abort(upload)
                    raise
        finally:
            # A successfully published file remains addressable even if a later
            # file fails validation; never silently lose a partial selection.
            self.items.extend(staged)

    def options(self, engine, text):
        if not self.items:
            return {}
        from xuloom.desktop.attachments import prepare_attachments
        blocks, items = prepare_attachments(engine.session_cwd, engine.session_id, [item["id"] for item in self.items])
        return {"input_blocks": [{"type": "text", "text": text}] + blocks, "attachments": items, "display_text": text}

    def consume_saved(self, engine):
        saved = {item["id"] for message in engine.messages for item in message.get("_attachments", [])}
        self.items = [item for item in self.items if item["id"] not in saved]

    def command(self, engine, text, console):
        argument = text.strip().partition(" ")[2].strip()
        if argument == "clear":
            self.items.clear()
        elif argument.startswith("remove "):
            target = argument.partition(" ")[2].strip()
            index = next((i for i, item in enumerate(self.items, 1) if item["id"] == target), None)
            index = int(target) if target.isdigit() else index
            if index is None or not 1 <= index <= len(self.items):
                raise ValueError("Unknown attachment; use /attach list")
            self.items.pop(index - 1)
        elif argument and argument != "list":
            self.add(engine, paths(argument))
        for index, item in enumerate(self.items, 1):
            console.print(f"{index}. {item['name']} · {item['size']} bytes · {item['id']}", markup=False)
        console.print("Attachments for the next message / 下一条消息附件: " + str(len(self.items)), markup=False)
