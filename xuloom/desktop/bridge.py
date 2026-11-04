"""JSON-lines RPC over stdio. One process owns one project session."""
import asyncio
import copy
import dataclasses
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from xuloom.branding import configuration_file

from xuloom.core.config import load_config, load_config_into_env, update_config
from xuloom.core.model_catalog import public_config, active_connection, save_connection, delete_connection, select_model, select_reasoning, discover_models, apply_connection_env, apply_catalog_env
from xuloom.security.permissions import ApprovalPolicy
from xuloom.desktop.activity import restore_activity
from xuloom.desktop.turns import TurnRecorder, conversation_history
from xuloom.features.file_changes import legacy_changes, preview_file, preview_image_file
from xuloom.features.workspace_files import list_directory, git_changes, git_file_diff, git_status
from xuloom.core.storage import session_directory, artifact_directory
from xuloom.skills.registry import SkillRegistry
from xuloom.agents.registry import AgentRegistry
from xuloom.protocols.mcp_config import MCPConfigStore
from xuloom.protocols.mcp_market import MCPMarket, materialize
from xuloom.protocols.mcp_bridge import MCPBridge, runtime_info


def pipe_reader(fd):
    if os.name != "nt":
        return lambda: os.read(fd, 65536)
    # A blocking CRT _read can hold descriptor locks needed by native DLL
    # initialization. ReadFile keeps the idle reader outside those CRT locks.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    read_file = kernel.ReadFile
    read_file.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    read_file.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    def read():
        buffer = ctypes.create_string_buffer(65536)
        count = wintypes.DWORD()
        if not read_file(handle, buffer, len(buffer), ctypes.byref(count), None):
            error = ctypes.get_last_error()
            if error in {109, 232}:
                return b""
            raise ctypes.WinError(error)
        return buffer.raw[:count.value]
    return read


class DesktopBridge:
    def __init__(self, emit, cwd=None):
        self.output = emit
        self.recorder = None
        self.cwd = Path(cwd or os.getcwd()).resolve()
        self.skills = SkillRegistry(self.cwd)
        self.agents = AgentRegistry(self.cwd)
        self.engine = None
        self.engine_initialization = asyncio.Lock()
        self.turn_task = None
        self.stop_requested = False
        self.pending = {}
        self.pending_kinds = {}
        self.capture_pending = {}
        self.approval_policy = ApprovalPolicy()
        self.collaboration_mode = "auto"
        self.execution_mode = "host"
        self.resume_id = None
        self.loaded_skills = []
        self.protocol_task = None
        self.protocol_ready = None
        self.protocol_shutdown = asyncio.Event()
        self.mcp_store = MCPConfigStore(configuration_file(self.cwd, "mcp_servers.json"))
        self.mcp_market = MCPMarket()
        self.mcp_requests = asyncio.Queue()
        self.mcp_names = set()
        self.mcp_stamp = None
        self.mcp_testing = False
        self.oauth_task = None
        self.oauth_callback = None
        self.oauth_name = None
        self.model_signature = None
        self.file_changes = []
        self.attachment_upload = None
        self.attachment_guard = asyncio.Lock()

    def emit(self, event):
        if self.recorder and self.recorder.accept(event):
            # Deliver the settled trace before turn_finished clears the UI's
            # active response index. Snapshots never enter model messages.
            self.output({"type": "turn_trace", "trace": copy.deepcopy(self.recorder.record)})
        if self.recorder and event.get("type") == "final":
            event = {**event, "text": self.recorder.record["answer"]}
        if event.get("type") == "file_changed":
            change = event["change"]
            if self.recorder:
                change = next(item for item in self.recorder.record["files"] if os.path.normcase(item["path"]) == os.path.normcase(change["path"]))
            self.file_changes = ([change] + [item for item in self.file_changes if os.path.normcase(item["path"]) != os.path.normcase(change["path"])])[:100]
            if self.engine:
                self.engine.tool_context["file_changes"] = self.file_changes
                self.engine.persist_session_state()
            event = {"type": "file_changed", "change": change}
        self.output(event)

    def config_stamp(self):
        try:
            stat = self.mcp_store.path.stat()
            document=json.loads(self.mcp_store.path.read_text(encoding="utf-8"))
            base=(stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            document={};base=None
        except (OSError,ValueError):
            return None
        # Include only the public file identity of this project's OAuth receipts.
        # CLI/another process can authorize a previously disconnected service;
        # its next turn must reconnect without touching model credentials.
        from xuloom.protocols.oauth_store import OAuthStore
        manifests=[]
        servers=self.mcp_store.skill_servers(manifests=manifests)
        if isinstance(document,dict) and isinstance(document.get("mcpServers",{}),dict): servers.update(document.get("mcpServers",{}))
        receipts=[]
        for name,server in sorted(servers.items()):
            if not isinstance(server,dict) or not isinstance(server.get("auth"),dict) or server["auth"].get("type")!="oauth":continue
            path=OAuthStore(self.mcp_store,name,{}).path
            try:
                stat=path.stat();identity=(stat.st_mtime_ns,stat.st_size)
            except FileNotFoundError:identity=None
            receipts.append((name,identity))
        return base,tuple(receipts),tuple(manifests)

    def mcp_snapshot(self):
        statuses = getattr(getattr(self.engine, "mcp_bridge", None), "statuses", {})
        changed = self.engine is not None and self.config_stamp() != self.mcp_stamp
        servers = []
        issues = []
        for server in self.mcp_store.snapshot(issues=issues):
            state = statuses.get(server["name"], {}) if not changed else {}
            if self.execution_mode == "workspace":
                state = {"status": "blocked", "error": "系统隔离模式不启动未受限的 MCP 服务"}
            servers.append({**server, "connection": state, "status": "invalid" if server.get("error") else "disabled" if server.get("enabled") is False else state.get("status", "pending")})
        return {"servers": servers, "configurationIssues": issues, "runtime": runtime_info(), "needsReload": changed, "builtinSearch": "DDGS 多搜索源", "project": str(self.cwd)}

    async def reload_mcp(self):
        if self.protocol_task is None:
            return
        if self.protocol_task.done():
            raise ValueError("MCP owner stopped; reopen this session")
        ready = asyncio.get_running_loop().create_future()
        self.mcp_testing = True
        try:
            await self.mcp_requests.put(ready)
            await asyncio.shield(ready)
        finally:
            self.mcp_testing = False

    def busy(self):
        return self.turn_task is not None and not self.turn_task.done() or bool(self.engine and getattr(self.engine, "_context_compacting", False))

    def require_idle(self):
        if self.attachment_upload is not None: raise ValueError("Wait for the attachment upload to finish or abort it")
        workers = self.engine and self.engine.tool_context["subagent_tasks"].tasks.values()
        commands = self.engine and self.engine.tool_context["command_sessions"].records.values()
        if self.busy() or (workers and any(not task.done() for task in workers)) or (commands and any(row["status"] == "running" for row in commands)):
            raise ValueError("Wait for running tasks or stop them before changing this session")

    def config(self):
        return public_config(load_config())

    async def sync_model(self, engine, force=False):
        cfg = load_config()
        config = public_config(cfg)
        provider = config["provider"]
        key = active_connection(cfg).get("api_key", "")
        signature = (provider, config["model"], config["baseUrl"], key, config["networkMode"],
            json.dumps(config.get("modelReasoning", {}).get(config["model"]), sort_keys=True))
        if not force and signature == self.model_signature:
            return
        if not config["connections"]:
            apply_catalog_env(cfg)
        else:
            apply_connection_env(active_connection(cfg))
        previous_client, previous_compactor = engine.client, engine.compactor
        engine.switch_model_runtime(provider=provider, model=config["model"], api_base_url=config["baseUrl"], api_key=key, network_mode=config["networkMode"], persist=False)
        await previous_client.close()
        if previous_compactor.client:
            await previous_compactor.client.close()
        self.model_signature = signature

    def capabilities(self):
        definitions = self.agents.definitions()
        builtins = [{"name": name, "description": item.get("when_to_use", ""), "scope": "builtin", "valid": True, "shadowed": False} for name, item in definitions.items() if "path" not in item]
        return {"skills": self.skills.discover(), "agents": builtins + [{k: v for k, v in item.items() if k != "disallowed_tools"} for item in self.agents.discover()], "settings": {**self.skills.settings(), "projectSkillsRoot": str(self.cwd / ".xuloom" / "skills"), "projectAgentsRoot": str(self.cwd / ".xuloom" / "agents"), "globalSkillsRoot": str(self.skills.home / ".xuloom" / "skills"), "globalAgentsRoot": str(self.skills.home / ".xuloom" / "agents")}}

    def sessions(self):
        root = session_directory(self.cwd)
        items = []
        if not root.exists():
            return items
        for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                messages = raw if isinstance(raw, list) else raw.get("messages", [])
                history = self.history(messages)
                title = next((m["text"][:65] for m in history if m["role"] == "user"), "Untitled session")
                items.append({"id": path.stem, "title": title, "updated": path.stat().st_mtime})
            except (OSError, ValueError, AttributeError):
                continue
        return items

    @staticmethod
    def history(messages, traces=()):
        return conversation_history(messages, traces)

    async def ask(self, kind, payload, *, explicit=False):
        if kind in {"permission", "plan_approval"} and self.approval_policy.mode == "full" and not explicit:
            return "allow"
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.pending_kinds[request_id] = "explicit_permission" if explicit else kind
        self.emit({"type": kind, "requestId": request_id, **payload})
        try:
            return await future
        finally:
            self.pending.pop(request_id, None)
            self.pending_kinds.pop(request_id, None)
            self.emit({"type": "interaction_resolved", "requestId": request_id})

    async def permission(self, request):
        return await self.ask("permission", {"request": dataclasses.asdict(request)},
            explicit=bool(request.classifier_input.get("forced_prompt_reasons")))

    async def capture(self, input):
        if os.environ.get("XULOOM_REMOTE") == "1":
            return {"ok": False, "error": "SSH screenshots must render on the execution host; no local browser fallback is allowed"}
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.capture_pending[request_id] = future
        self.output({"type": "browser_capture", "requestId": request_id, "input": input})
        try:
            return await asyncio.wait_for(future, 45)
        finally:
            self.capture_pending.pop(request_id, None)
            self.output({"type": "browser_capture_cancel", "requestId": request_id})

    async def ensure_engine(self, write=False):
        async with self.engine_initialization:
            if write and not getattr(self, "owned_worker_controller", False) and self.resume_id:
                from xuloom.core.session import SessionManager
                with SessionManager(cwd=str(self.cwd)) as manager:
                    state = manager.load_session_state(self.resume_id)
                if state.get("metadata", {}).get("delegation", {}).get("authority_required"):
                    raise ValueError("此会话由原主代理控制，只能查看或创建独立分支；请返回主会话继续执行。")
            return await self._ensure_engine(write)

    def _create_engine(self):
        from xuloom.core.engine import QueryEngine
        return QueryEngine(cwd=str(self.cwd), permission_handler=self.permission, approval_policy=self.approval_policy,
                           collaboration_mode=self.collaboration_mode, execution_mode=self.execution_mode)

    def _configure_engine(self, engine):
        """Execution hosts may constrain their role before any model request."""

    async def _connect_mcp(self, engine):
        return {} if engine.execution_sandbox.enabled else await engine.mcp_bridge.load_and_connect()

    async def _ensure_engine(self, write=False):
        if self.engine is not None:
            if write:
                self.engine.require_writer()
            await self.sync_model(self.engine)
            if self.protocol_ready is not None:
                await asyncio.shield(self.protocol_ready)
            if self.config_stamp() != self.mcp_stamp:
                await self.reload_mcp()
            self._configure_engine(self.engine)
            return self.engine
        load_config_into_env()
        from xuloom.tools.lsp_tool import LspTool
        from xuloom.protocols.lsp_manager import LSPManager
        engine = self._create_engine()
        try:
            if self.resume_id and not engine.load_session(self.resume_id):
                raise ValueError("Session could not be resumed")
            if write:
                engine.require_writer()
            # History restores conversation state; current saved settings determine
            # how its next request connects, including changes made in another tab.
            await self.sync_model(engine, force=True)
            for record in engine.subagent_registry:
                if record.get("status") == "running":
                    record["status"] = "interrupted"
            for member in engine.team_manager.get_non_lead_members():
                member.is_active = False
            engine.set_permission_handler(self.permission)
            engine.browser_manager.capture_callback = None if os.environ.get("XULOOM_REMOTE") == "1" else self.capture
            engine.tool_context.update(skill_registry=self.skills, agent_registry=self.agents, capability_event=self.emit, ask_user_callback=lambda questions: self.ask("questions", {"questions": questions}), plan_approval_callback=lambda content: self.ask("plan_approval", {"content": content}))
            lsp = LspTool()
            engine.lsp_manager = LSPManager()
            engine.tool_context["lsp_manager"] = engine.lsp_manager
            lsp.context = engine.tool_context
            engine.latent_tools[lsp.name] = lsp
            self._configure_engine(engine)
        except BaseException:
            await engine.aclose()
            raise
        self.engine = engine
        self.protocol_ready = asyncio.get_running_loop().create_future()
        self.protocol_task = asyncio.create_task(self.protocol_lifetime(engine))
        await asyncio.shield(self.protocol_ready)
        self._configure_engine(engine)
        self.emit({"type": "session_started", "sessionId": engine.session_id, "model": engine.primary_model})
        return engine

    async def protocol_lifetime(self, engine):
        # MCP's context managers must be entered and exited by the same task.
        ready = self.protocol_ready
        try:
            while True:
                try:
                    active_mcp = self.mcp_names.intersection(engine.available_tools)
                    for name in self.mcp_names:
                        engine.available_tools.pop(name, None)
                        engine.latent_tools.pop(name, None)
                    self.mcp_names.clear()
                    if getattr(engine, "mcp_bridge", None):
                        await engine.mcp_bridge.cleanup()
                    engine.mcp_bridge = MCPBridge(str(self.mcp_store.path))
                    self.mcp_stamp = self.config_stamp()
                    tools = await self._connect_mcp(engine)
                    self.mcp_names = engine.register_mcp_tools(tools)
                    engine.activate_tools(active_mcp.intersection(self.mcp_names))
                    self.emit({"type": "mcp_changed", "snapshot": await asyncio.to_thread(self.mcp_snapshot)})
                    if not ready.done():
                        ready.set_result(True)
                except Exception:
                    self.emit({"type": "log", "text": "MCP configuration or connection failed; check the MCP panel"})
                    if not ready.done():
                        if ready is self.protocol_ready:
                            ready.set_result(True)
                        else:
                            ready.set_exception(ValueError("MCP configuration or connection failed; check the MCP panel"))
                ready = await self.mcp_requests.get()
                if ready is None:
                    break
        finally:
            if getattr(engine, "mcp_bridge", None):
                await engine.mcp_bridge.cleanup()

    async def turn(self, text, selected_skills, mode, attachment_ids=None, prepared=None, resume_trace=None):
        receipt = None
        self.recorder = TurnRecorder(resume_trace)
        if resume_trace:
            self.emit({"type": "turn_resumed", "trace": copy.deepcopy(self.recorder.record)})
        self.emit({"type": "turn_started"})
        try:
            # Validate all selections before creating a turn or emitting loaded events.
            if prepared is None:
                for name in selected_skills:
                    self.skills.read(name)
            engine = await self.ensure_engine(write=True)
            from xuloom.desktop.attachments import prepare_attachments
            attachment_blocks, attachment_items = prepared[1] if prepared is not None else prepare_attachments(self.cwd, getattr(engine, "session_id", ""), attachment_ids or [])
            display_text = text
            if resume_trace:
                position = next(i for i, trace in enumerate(engine.turn_traces) if trace["id"] == resume_trace["id"])
                engine.turn_traces[position] = self.recorder.record
            else:
                engine.turn_traces.append(self.recorder.record)
            engine.active_turn_trace = self.recorder.record
            engine.set_mode(mode)
            options = {"input_blocks": [{"type": "text", "text": text}] + attachment_blocks, "attachments": attachment_items, "display_text": display_text} if attachment_items else {}
            if resume_trace: options["resume"] = True
            async for event in engine.submit_message(text, sys_print_callback=lambda text: self.emit({"type": "log", "text": str(text)}), selected_skills=selected_skills, **options):
                if event.get("type") == "final": receipt = event.get("result")
                if self.stop_requested and event.get("type") == "final":
                    continue
                if event.get("type") == "final" and not event.get("result", {}).get("ok", False):
                    receipt = event.get("result", {})
                    self.emit({"type": "turn_error", "message": re.sub(r"\[/?bold red\]", "", event["text"]) + " (" + receipt.get("reason", "unknown") + ")", "result": receipt})
                    continue
                self.emit(event)
            self.emit({"type": "sessions_changed", "sessions": self.sessions()})
        except asyncio.CancelledError:
            self.stop_requested = True
        except Exception as exc:
            self.emit({"type": "turn_error", "message": str(exc)})
        finally:
            if self.stop_requested:
                if self.engine and self.recorder.record["pendingText"].strip():
                    from xuloom.core.messages import create_assistant_message
                    partial = self.recorder.record["pendingText"]
                    # The prefix is already in the model transcript from the
                    # earlier stop. Persist only newly received text this time.
                    if self.recorder.answer_prefix and partial.startswith(self.recorder.answer_prefix):
                        partial = partial[len(self.recorder.answer_prefix):]
                    from xuloom.desktop.turns import visible_text
                    if partial.strip() and (not self.engine.messages or visible_text(self.engine.messages[-1]) != partial):
                        self.engine.messages.append(create_assistant_message(partial, stop_reason="interrupted"))
                        self.engine._sync_message_context()
                self.emit({"type": "turn_cancelled"})
            # Settle the trace before saving, but publish completion only after
            # that final save. JSONL callers must not miss a late storage error.
            finished = {"type": "turn_finished"}
            self.recorder.accept(finished)
            if self.engine and self.engine.session_manager.owns_writer(self.engine.session_id):
                self.engine.active_turn_trace = None
                if not self.engine.persist_session_state():
                    receipt = {"ok": False, "reason": "persistence_error"}
                    self.emit({"type": "turn_error", "message": "本轮过程记录保存失败；请检查存储空间和权限。", "result": {"ok": False, "reason": "persistence_error"}})
            self.output({"type": "turn_trace", "trace": copy.deepcopy(self.recorder.record)})
            self.output(finished)
            self.recorder = None
        return receipt or {"ok": False, "reason": "cancelled" if self.stop_requested else "error"}

    async def dispatch(self, method, params):
        if method.startswith("scaling."):
            from xuloom.scaling.control import invoke
            return await invoke(method, params)
        if method in {"context.get", "context.compact"}:
            from xuloom.context.compaction import status
            if method == "context.compact": self.require_idle()
            engine = await self.ensure_engine(write=method == "context.compact")
            if method == "context.compact":
                engine.abort_event.clear()
                try:
                    await engine._apply_layered_compaction_if_needed(lambda _: None, force=True, focus=str(params.get("focus") or "")[:4000])
                except asyncio.CancelledError:
                    raise ValueError("上下文压缩已取消，原始记录已保留") from None
            return status(engine)
        if method == "runtime.info":
            import socket
            from xuloom import __version__
            return {"protocol": 1, "version": __version__, "hostname": socket.gethostname(),
                "platform": sys.platform, "cwd": str(self.cwd), "attachments": 1, "transport": "ssh" if os.environ.get("XULOOM_REMOTE") == "1" else "stdio"}
        if method.startswith("attachments."):
            from xuloom.desktop import attachment_transfer
            return await attachment_transfer.dispatch(self, method, params)
        if method.startswith("remote."):
            if method in {"remote.references", "remote.forget"}:
                from xuloom.remote import navigation
                if method == "remote.references": return {"references": navigation.references()}
                return navigation.forget(params.get("id"), params.get("version"), confirm=params.get("confirm"))
            from xuloom.remote import config as remote_config
            if method == "remote.profiles": return {"connections": remote_config.profiles()}
            if method == "remote.save": return remote_config.save(params)
            if method == "remote.remove":
                remote_config.remove(params.get("id")); return {"removed": True}
            if method == "remote.check":
                from xuloom.remote.ssh import SSHClient
                client = SSHClient(remote_config.resolve(params.get("id")))
                try: return await client.start()
                finally: await client.aclose()
            if method == "remote.models":
                from xuloom.remote.ssh import SSHClient
                client = SSHClient(remote_config.resolve(params.get("id")))
                try:
                    await client.start()
                    config = await client.request("config.get")
                    return {"connections": config["connections"]}
                finally:
                    await client.aclose()
            raise ValueError("Unknown SSH configuration operation")
        if method == "sessions.list":
            return self.sessions()
        if method == "sessions.create":
            from xuloom.core.session import SessionManager
            identity = str(uuid.uuid4())
            with SessionManager(cwd=self.cwd) as manager:
                manager.acquire_writer(identity)
                if not manager.save_session_state(identity, [], {}):
                    raise OSError("Could not save the new conversation")
            return {"sessionId": identity, "project": str(self.cwd)}
        if method == "session.status":
            from xuloom.core.session import SessionManager
            from xuloom.core.session_lock import SessionLock, SessionBusyError
            identity = params.get("sessionId")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                raise ValueError("Invalid session ID")
            path = session_directory(self.cwd) / (identity + ".json")
            raw = json.loads(path.read_text(encoding="utf-8"))
            owned = self.engine is not None and self.engine.session_id == identity and self.engine.session_manager.owns_writer(identity)
            writer = "current" if owned else "available"
            if not owned:
                with SessionManager(cwd=self.cwd) as manager:
                    probe = SessionLock(manager.lock_directory, identity)
                    try: probe.acquire()
                    except SessionBusyError: writer = "elsewhere"
                    finally: probe.close()
            metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
            return {"sessionId": identity, "writer": writer, "busy": self.busy() if owned else None,
                "lastResult": metadata.get("last_run_result"), "revision": raw.get("revision") if isinstance(raw, dict) else None}
        if method == "browser.capture_result":
            future = self.capture_pending.get(params.get("requestId"))
            if future is None or future.done():
                return False
            future.set_result(params.get("result"))
            return True
        if method.startswith("goal."):
            from xuloom.features import goals
            if method == "goal.get":
                if self.engine and self.engine.session_manager.owns_writer(self.engine.session_id): return copy.deepcopy(self.engine.goal)
                from xuloom.core.session import SessionManager
                with SessionManager(cwd=self.cwd) as manager:
                    sid = self.engine.session_id if self.engine else self.resume_id
                    raw = manager.load_session_state(sid) if sid else {}
                    return goals.snapshot(manager, sid, raw.get("metadata", {}).get("goal"))
            if method == "goal.control":
                engine = await self.ensure_engine(write=True)
                result = await goals.control(engine, params["action"], params.get("reason", ""))
                self.emit({"type": "goal_changed", "goal": result})
                return result
            if method == "goal.update":
                if not isinstance(params.get("objective"), str) or not params["objective"].strip():
                    raise ValueError("请填写目标")
                if self.busy(): await self.dispatch("stop", {})
                self.require_idle()
                engine = await self.ensure_engine(write=True)
                result = goals.update_objective(engine, params["objective"])
                self.emit({"type": "goal_changed", "goal": result})
                if params.get("resume"):
                    self.stop_requested = False
                    self.turn_task = asyncio.create_task(self.goal_turn(engine))
                return result
            self.require_idle()
            engine = await self.ensure_engine(write=True)
            if method == "goal.create":
                result = goals.create(engine, params["objective"], params["criteria"], checks=params.get("checks"),
                    rounds=params.get("rounds", 20), seconds=params.get("seconds", 1800),
                    output_tokens=params.get("output_tokens", 50000), skills=params.get("skills"), continuous=True)
                engine.set_mode("normal")
                if not engine.persist_session_state():
                    raise OSError("目标已创建，但执行模式保存失败；请重新打开会话检查目标状态")
                self.emit({"type": "goal_changed", "goal": result})
                return result
            if method == "goal.run":
                if engine.goal and not engine.goal.get("continuous"):
                    engine.goal.update(continuous=True, budget=dict(rounds=None, seconds=None, output_tokens=None))
                    if engine.goal["status"] == "budget_exhausted": engine.goal["status"] = "paused"
                    goals.save(engine)
                self.stop_requested = False
                self.turn_task = asyncio.create_task(self.goal_turn(engine))
                return {"started": True}
            raise ValueError("Unknown goal operation")
        if method.startswith("schedule."):
            from xuloom.features import schedules
            if method == "schedule.list": return schedules.list_items()
            if method == "schedule.due": return schedules.list_items(due=True)
            if method == "schedule.control": return schedules.change(params["id"], params["action"])
            if method == "schedule.add":
                engine = await self.ensure_engine(write=True)
                return schedules.add(engine, params["prompt"], due_at=params.get("due_at", time.time()), interval=params.get("interval"), skills=params.get("skills"),
                    trigger=params.get("trigger"), request_id=params.get("request_id"))
            if method == "schedule.deliver":
                if self.busy() or self.mcp_testing: return {"status": "queued"}
                engine = await self.ensure_engine(write=True)
                async def execute(text, skills):
                    self.stop_requested = False
                    self.emit({"type": "managed_turn_started", "text": text})
                    return await self.turn(text, skills, "normal")
                async def delivery():
                    try: await schedules.deliver(engine, params["id"], execute)
                    except Exception as error:
                        self.emit({"type": "turn_error", "message": str(error)})
                        self.emit({"type": "turn_finished"})
                self.turn_task = asyncio.create_task(delivery())
                return {"started": True}
            raise ValueError("Unknown schedule operation")
        if method == "storage.mutate":
            from xuloom.core.session_mutation import mutate
            return mutate(params)
        if method == "chat.resolve":
            from xuloom.features.chat_links import resolve_link
            return resolve_link(params.get("url"), require_openable=True)
        if method == "config.get":
            return self.config()
        if method in {"media.connections", "media.save", "media.remove", "media.models", "media.saveModel", "media.removeModel"}:
            from xuloom.media import config as media_config
            if method == "media.connections":
                return {"connections": media_config.public_connections()}
            if method == "media.models":
                return {"models": media_config.public_models()}
            self.require_idle()
            if method == "media.saveModel":
                return {"models": await asyncio.to_thread(media_config.save_model, params.get("model"))}
            if method == "media.removeModel":
                if params.get("confirm") is not True: raise ValueError("请确认移除生成模型")
                return {"models": await asyncio.to_thread(media_config.remove_model, params.get("connectionId"), params.get("modelId"))}
            if method == "media.remove" and params.get("confirm") is not True:
                raise ValueError("请确认移除媒体连接；这不会取消供应商已有任务")
            rows = await asyncio.to_thread(media_config.save, params.get("connection")) if method == "media.save" else await asyncio.to_thread(media_config.remove, params.get("id"))
            return {"connections": rows}
        if method == "media.jobs":
            from xuloom.media.jobs import listing
            sid = self.engine.session_id if self.engine else self.resume_id
            return {"jobs": await asyncio.to_thread(listing, {"session_cwd": str(self.cwd), "session_id": sid}) if sid else []}
        if method == "media.control":
            self.require_idle()
            if params.get("action") not in {"status", "cancel"} or not (self.engine or self.resume_id):
                raise ValueError("请打开已有会话，并选择查询或取消已有媒体任务")
            engine = await self.ensure_engine(write=True)
            from xuloom.media.jobs import load
            load(engine.tool_context, params.get("jobId"))
            self.turn_task = asyncio.create_task(self.media_control(engine, params["action"], params["jobId"]))
            return {"started": True}
        if method == "collaboration.get":
            return {"collaborationMode": self.collaboration_mode}
        if method == "execution.cleanup":
            self.require_idle()
            from xuloom.security.sandbox_cleanup import retry
            from xuloom.security.execution import ExecutionSandbox
            retry(str(self.cwd), params.get("id"))
            return ExecutionSandbox(str(self.cwd), self.execution_mode).status()
        if method in {"execution.get", "execution.set"}:
            from xuloom.security.execution import ExecutionSandbox
            if method == "execution.set":
                self.require_idle()
                mode = params.get("mode")
                candidate = ExecutionSandbox(str(self.cwd), mode)
                if candidate.enabled and not candidate.status()["available"]:
                    raise ValueError(candidate.status()["message"])
                if self.engine:
                    self.engine.require_writer()
                    if self.engine.sandbox_manager.active_sandbox:
                        raise ValueError("请先退出工作区副本隔离，再切换系统执行隔离")
                    previous = self.engine.execution_sandbox.mode
                    self.engine.execution_sandbox.mode = mode
                    if not self.engine.persist_session_state():
                        self.engine.execution_sandbox.mode = previous
                        raise OSError("执行隔离设置保存失败")
                    if candidate.enabled:
                        await asyncio.gather(*(server.stop() for server in getattr(getattr(self.engine, "lsp_manager", None), "servers", {}).values()))
                    await self.reload_mcp()
                elif self.resume_id:
                    from xuloom.core.session import SessionManager
                    with SessionManager(cwd=str(self.cwd)) as manager:
                        manager.acquire_writer(self.resume_id)
                        raw = manager.load_session_state(self.resume_id)
                        if not raw.get("_valid"):
                            raise ValueError("Session could not be resumed")
                        raw["metadata"]["execution_mode"] = mode
                        if not manager.save_session_state(self.resume_id, raw["messages"], raw["metadata"]):
                            raise OSError("执行隔离设置保存失败")
                self.execution_mode = mode
            result = ExecutionSandbox(str(self.cwd), self.execution_mode).status()
            return {"executionMode": self.execution_mode, **result}
        if method == "collaboration.set":
            self.require_idle()
            from xuloom.agents.collaboration import policy
            mode = params.get("mode")
            profile = policy(mode, self.skills.settings())
            if self.engine:
                self.engine.set_collaboration_mode(mode)
            elif self.resume_id:
                # Changing a local preference must not require an API connection.
                from xuloom.core.session import SessionManager
                with SessionManager(cwd=str(self.cwd)) as manager:
                    manager.acquire_writer(self.resume_id)
                    raw = manager.load_session_state(self.resume_id)
                    if not raw.get("_valid"):
                        raise ValueError("Session could not be resumed")
                    raw["metadata"]["collaboration_mode"] = mode
                    raw["metadata"].pop("collaboration_run", None)
                    if not manager.save_session_state(self.resume_id, raw["messages"], raw["metadata"]):
                        raise OSError("协作档位保存失败: " + manager.last_save_error)
            self.collaboration_mode = mode
            value = {"collaborationMode": mode, "limits": profile}
            self.emit({"type": "collaboration_mode_changed", **value})
            return value
        if method == "permissions.get":
            return {"permissionMode": self.approval_policy.mode}
        if method == "permissions.set":
            self.approval_policy.set_mode(params.get("mode"))
            if self.engine and self.approval_policy.mode == "ask":
                self.engine.permission_manager.always_allow_tools.clear()
                if self.engine.session_manager.owns_writer(self.engine.session_id):
                    self.engine.persist_session_state()
            if self.approval_policy.mode == "full":
                for request_id, future in list(self.pending.items()):
                    if self.pending_kinds.get(request_id) in {"permission", "plan_approval"} and not future.done():
                        future.set_result("allow")
            self.emit({"type": "permission_mode_changed", "permissionMode": self.approval_policy.mode})
            return {"permissionMode": self.approval_policy.mode}
        if method == "config.models":
            return await discover_models(load_config(), params)
        if method == "mcp.list":
            return await asyncio.to_thread(self.mcp_snapshot)
        if method in {"mcp.oauth.cancel","mcp.oauth.callback"}:
            if not self.oauth_task or self.oauth_task.done() or self.oauth_name!=params.get("name"):
                raise ValueError("此服务没有等待中的 OAuth 登录")
            if method=="mcp.oauth.callback":
                if not self.oauth_callback.accept(params.get("url","")):
                    raise ValueError("回调不匹配此登录，尚未使用授权码")
                return {"accepted":True}
            self.oauth_task.cancel()
            await asyncio.gather(self.oauth_task,return_exceptions=True)
            return {"status":"cancelled"}
        if method in {"mcp.oauth.login","mcp.oauth.logout"}:
            self.require_idle()
            if self.execution_mode=="workspace": raise ValueError("系统隔离模式不在宿主执行 OAuth 登录或撤销")
            if self.mcp_testing: raise ValueError("请先完成或取消当前 MCP 操作")
            if not runtime_info()["available"]: raise ValueError(runtime_info()["message"])
            from xuloom.protocols.mcp_config import validate_server
            from xuloom.protocols.oauth_store import OAuthStore
            from xuloom.protocols.oauth_callback import OAuthCallback
            from xuloom.protocols.mcp_oauth import login, logout
            name=params.get("name","")
            raw=(await asyncio.to_thread(self.mcp_store.read,include_skills=True))["mcpServers"].get(name)
            configured=validate_server(raw)
            if configured.get("auth",{}).get("type")!="oauth": raise ValueError("先为此 HTTP 服务选择 OAuth 登录")
            store=OAuthStore(self.mcp_store,name,configured)
            self.mcp_testing=True
            try:
                if method=="mcp.oauth.logout":
                    result=await logout(store)
                else:
                    self.oauth_callback=OAuthCallback();self.oauth_name=name
                    async def notify(event):
                        # Deliberately bypass turn recorders and model context.
                        self.output({"type":"mcp_oauth_authorize","name":name,**event})
                    self.oauth_task=asyncio.create_task(login(store,self.oauth_callback,notify))
                    try: result=await self.oauth_task
                    except asyncio.CancelledError: result={"status":"cancelled"}
                return {**result,"snapshot":await asyncio.to_thread(self.mcp_snapshot)}
            finally:
                self.oauth_task=None;self.oauth_callback=None;self.oauth_name=None
                self.mcp_testing=False
        if method == "mcp.market":
            return await self.mcp_market.search(params.get("query", ""), params.get("source", "builtin"), params.get("cursor", ""))
        if method in {"mcp.install", "mcp.save", "mcp.toggle", "mcp.remove", "mcp.reload", "mcp.test"}:
            if self.execution_mode == "workspace" and method == "mcp.test":
                raise ValueError("系统隔离模式不启动未受限的 MCP 服务；未在宿主执行。")
            self.require_idle()
            if self.mcp_testing:
                raise ValueError("Wait for the MCP connection test to finish")
            name = params.get("name", "")
            if method == "mcp.install":
                entry = self.mcp_market.entries.get(params.get("id"))
                if not entry:
                    raise ValueError("Refresh the market before installing this entry")
                await asyncio.to_thread(self.mcp_store.save, name or entry["id"], materialize(entry, params.get("values", {})))
            elif method == "mcp.save":
                await asyncio.to_thread(self.mcp_store.save, name, params.get("config", {}), existing=params.get("existing", False))
            elif method == "mcp.toggle":
                await asyncio.to_thread(self.mcp_store.toggle, name, params.get("enabled"))
            elif method == "mcp.remove":
                await asyncio.to_thread(self.mcp_store.remove, name)
            elif method == "mcp.reload":
                await self.reload_mcp()
            elif method == "mcp.test":
                if name not in self.mcp_store.read(include_skills=True)["mcpServers"]:
                    raise ValueError("Unknown MCP server")
                self.mcp_testing = True
                bridge = MCPBridge(str(self.mcp_store.path))
                try:
                    await bridge.load_and_connect(only=name, include_disabled=True)
                    return {"runtime": runtime_info(), **bridge.statuses.get(name, {})}
                finally:
                    await bridge.cleanup()
                    self.mcp_testing = False
            return await asyncio.to_thread(self.mcp_snapshot)
        if method == "initialize":
            if self.attachment_upload is not None: raise ValueError("Finish the attachment upload before switching sessions")
            self.approval_policy.set_mode(params.get("permissionMode", "ask"))
            sid = params.get("sessionId")
            if sid and not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
                raise ValueError("Invalid session ID")
            self.resume_id = sid
            messages = []
            raw = {"messages": []}
            if sid:
                raw = json.loads((session_directory(self.cwd) / (sid + ".json")).read_text(encoding="utf-8"))
                messages = raw if isinstance(raw, list) else raw.get("messages", [])
            activity, workers = restore_activity(raw, self.cwd)
            saved_changes = raw.get("metadata", {}).get("file_changes", []) if isinstance(raw, dict) else []
            saved_paths = {os.path.normcase(item["path"]) for item in saved_changes}
            self.file_changes = saved_changes + [item for item in legacy_changes(activity, self.cwd) if os.path.normcase(item["path"]) not in saved_paths]
            traces = raw.get("metadata", {}).get("turn_traces", []) if isinstance(raw, dict) else []
            self.collaboration_mode = raw.get("metadata", {}).get("collaboration_mode", "auto") if isinstance(raw, dict) else "auto"
            self.execution_mode = raw.get("metadata", {}).get("execution_mode", "host") if isinstance(raw, dict) else "host"
            controlled = bool(raw.get("metadata", {}).get("delegation", {}).get("authority_required")) if isinstance(raw, dict) else False
            controlled_model = raw.get("metadata", {}).get("delegation", {}).get("remote_worker", {}).get("model_binding", {}).get("model") if controlled else None
            return {"controlledExecution": controlled, "controlledModel": controlled_model, "project": str(self.cwd), "persistedId": sid, "executionMode": self.execution_mode, "collaborationMode": self.collaboration_mode, "permissionMode": self.approval_policy.mode, "config": self.config(), "capabilities": self.capabilities(), "sessions": self.sessions(), "history": self.history(messages, traces), "activity": activity, "workers": workers, "fileChanges": self.file_changes}
        if method == "files.read":
            return preview_file(self.cwd, params.get("path"), self.file_changes)
        if method == "files.reference":
            from xuloom.features.workspace_files import inspect_reference
            return inspect_reference(self.cwd, params.get("path"), self.file_changes, params.get("basePath"))
        if method in {"files.htmlInfo", "files.htmlChunk"}:
            from xuloom.features import html_preview
            if method == "files.htmlInfo":
                return await asyncio.to_thread(html_preview.info, self.cwd, params.get("path"), self.file_changes, params.get("asset"))
            return await asyncio.to_thread(html_preview.read, self.cwd, params.get("path"), self.file_changes,
                params.get("asset"), params.get("version"), params.get("offset", 0), params.get("length", 262144))
        if method in {"files.mediaInfo", "files.mediaChunk"}:
            from xuloom.media import preview
            if method == "files.mediaInfo":
                return await asyncio.to_thread(preview.info, self.cwd, params.get("path"), self.file_changes)
            return await asyncio.to_thread(preview.read, self.cwd, params.get("path"), self.file_changes,
                params.get("version"), params.get("offset", 0), params.get("length", 262144))
        if method in {"session.transcript", "session.transcriptImage"}:
            from xuloom.features.agent_history import inspect
            sid = self.engine.session_id if self.engine else self.resume_id
            if not sid:
                raise ValueError("请先打开已保存的会话")
            if method == "session.transcriptImage" and params.get("imageIndex") is None:
                raise ValueError("请选择历史记录中的图片")
            return await inspect(self.cwd, sid, agent_id=params.get("agentId"), cursor=params.get("cursor"),
                limit=params.get("limit", 40), image_index=params.get("imageIndex") if method.endswith("Image") else None,
                version=params.get("version"))
        if method == "session.summary":
            from xuloom.features.session_summary import read_summary
            sid = self.engine.session_id if self.engine else self.resume_id
            if not sid:
                return {"version": 1, "scope": "turn", "turnId": None, "files": [], "sources": [], "tools": [], "warnings": [], "legacySourcesUnavailable": False}
            return await asyncio.to_thread(read_summary, self.cwd, sid,
                turn_id=params.get("turnId"), all_turns=params.get("all") is True)
        if method == "files.image":
            return await asyncio.to_thread(preview_image_file, self.cwd, params.get("path"), self.file_changes, params.get("basePath"))
        if method == "media.read":
            from xuloom.context.tool_media import image_preview
            sid = self.engine.session_id if self.engine else self.resume_id
            if not sid or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
                raise ValueError("请先打开保存此图像的会话")
            return await asyncio.to_thread(image_preview, artifact_directory(self.cwd) / sid,
                params.get("id"), thumbnail=params.get("thumbnail") is True)
        if method == "files.undo":
            self.require_idle()
            if params.get("confirm") is not True:
                raise ValueError("请确认撤销文件最近一次写入")
            engine = await self.ensure_engine(write=True)
            return await engine.undo_file(params.get("revisionId"))
        if method == "files.list":
            return list_directory(self.cwd, params.get("path", ""))
        if method == "git.status":
            return await git_status(self.cwd)
        if method == "files.gitDiff":
            return await git_file_diff(self.cwd, params.get("path"))
        if method == "capabilities.list":
            return self.capabilities()
        if method == "capabilities.create":
            self.require_idle()
            from xuloom.desktop.capability_config import create_definition
            create_definition(self.skills, params)
            return self.capabilities()
        if method == "capabilities.directory":
            kind = params.get("kind")
            if kind not in {"skills", "agents"}:
                raise ValueError("Unknown capability kind")
            root = self.skills.home if params.get("scope") == "global" else self.cwd
            directory = root / ".xuloom" / kind
            directory.mkdir(parents=True, exist_ok=True)
            return str(directory)
        if method == "capabilities.example":
            self.require_idle()
            files = {
                self.cwd / ".xuloom/skills/project-review/SKILL.md": "---\nname: project-review\ndescription: Review project structure, implementation and tests.\n---\n\nRead references/checklist.md before reviewing. Inspect relevant source and report concrete findings with file paths. Do not modify code unless explicitly requested.\n",
                self.cwd / ".xuloom/skills/project-review/references/checklist.md": "# Review checklist\n\nCheck correctness, error handling, project boundaries and meaningful test coverage. Clearly distinguish verified findings from uncertainty.\n",
                self.cwd / ".xuloom/agents/reviewer.md": "---\nname: reviewer\ndescription: Review code independently and report concrete issues.\nmodel: inherit\ntools: Read, Grep, Glob, Skill\nskills: [project-review]\n---\n\nInspect the assigned code using the project-review skill. Report actionable issues with file paths and reasons. Do not modify files.\n",
            }
            if any(path.exists() for path in files):
                raise ValueError("示例文件已存在；请导入或刷新现有定义，不会覆盖文件。")
            for path, body in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(body)
            return self.capabilities()
        if method == "capabilities.import":
            self.require_idle()
            kind = params.get("kind")
            if kind not in {"skills", "agents"}:
                raise ValueError("Unknown capability kind")
            path = Path(params["path"]).resolve()
            if not path.is_dir():
                raise ValueError("Choose an existing directory")
            if kind == "skills" and not any(path.rglob("SKILL.md")):
                raise ValueError("This directory contains no SKILL.md")
            if kind == "agents" and not any(path.glob("*.md")):
                raise ValueError("Choose a directory containing agent Markdown files")
            key = "skill_roots" if kind == "skills" else "agent_roots"
            roots = self.skills.settings().get(key, [])
            if str(path) not in roots:
                self.skills.update(**{key: roots + [str(path)]})
            return self.capabilities()
        if method == "capabilities.toggle":
            raise ValueError("Skills are available automatically; select a skill to use it")
        if method == "skills.read":
            return self.skills.read(params["name"])
        if method == "agents.read":
            agent = self.agents.resolve(params["name"])
            if agent.get("path"):
                return Path(agent["path"]).read_text(encoding="utf-8-sig")
            return str(agent.get("system_prompt", agent.get("description", "")))
        if method == "prompt.resume":
            self.require_idle()
            engine = await self.ensure_engine(write=True)
            if getattr(engine, "goal", None) and engine.goal["status"] not in {"completed", "cancelled", "budget_exhausted"}:
                raise ValueError("请使用目标条的恢复操作继续当前目标")
            human = next((message for message in reversed(engine.messages) if message.get("role") == "user"
                and message.get("_origin", "human") == "human" and not message.get("_is_meta") and not message.get("_is_tool_result")
                and not any(block.get("type") == "tool_result" for block in message.get("content", []) if isinstance(block, dict))), None)
            previous = next((trace for trace in reversed(engine.turn_traces)
                if trace.get("userMessageId") == (human or {}).get("_uuid")), None)
            if not human or not previous or previous.get("status") not in {"cancelled", "interrupted", "running"}:
                raise ValueError("当前没有可继续的已停止任务")
            self.stop_requested = False
            self.turn_task = asyncio.create_task(self.turn("", [], engine.get_mode(), resume_trace=copy.deepcopy(previous)))
            return {"started": True}
        if method == "prompt":
            if self.attachment_upload is not None: raise ValueError("Attachment upload has not finished; task was not submitted")
            if self.busy() or self.mcp_testing:
                raise ValueError("A turn or MCP connection operation is already running")
            attachment_ids = params.get("attachments", [])
            text = str(params.get("text", "")).strip() or ("请查看这些附件。" if attachment_ids else "")
            if not text:
                raise ValueError("Message is empty")
            if params.get("mode", "normal") not in {"normal", "plan"}:
                raise ValueError("Invalid mode")
            selections = [(name, self.skills.read(name)) for name in params.get("skills", [])]
            from xuloom.desktop.attachments import prepare_attachments
            prepared_attachments = prepare_attachments(self.cwd, self.resume_id or (self.engine.session_id if self.engine else ""), attachment_ids)
            if params.get("replaceMessageId"):
                self.require_idle()
                engine = await self.ensure_engine(write=True)
                from xuloom.desktop.prompt_edit import replace_latest_turn
                replace_latest_turn(engine, params["replaceMessageId"])
                self.emit({"type": "turn_restarted", "history": self.history(engine.messages, engine.turn_traces), "text": text, "attachments": prepared_attachments[1]})
            await self.ensure_engine(write=True)
            if self.busy():
                raise ValueError("A turn is already running")
            self.stop_requested = False
            self.turn_task = asyncio.create_task(self.turn(text, params.get("skills", []), params.get("mode", "normal"), attachment_ids, (selections, prepared_attachments)))
            return {"started": True}
        if method == "respond":
            future = self.pending.get(params["requestId"])
            if future is None or future.done():
                raise ValueError("This interaction has already ended")
            future.set_result(params.get("answer"))
            return True
        if method == "shutdown":
            await self.close()
            return {"closed": True}
        if method == "stop":
            from xuloom.desktop import attachment_transfer
            await attachment_transfer.cancel(self)
            self.stop_requested = self.busy()
            if self.engine and getattr(self.engine, "goal", None) and self.engine.goal["status"] == "active":
                self.engine.goal["status"], self.engine.goal["reason"] = "paused", "Stopped by user"
            for future in self.pending.values():
                future.cancel()
            for future in list(self.capture_pending.values()):
                future.cancel()
            cleanup = []
            if self.engine:
                cleanup.append(asyncio.create_task(self.engine.request_abort()))
                cleanup.append(asyncio.create_task(self.engine.tool_context["command_sessions"].close()))
            # Stop the parent immediately as well as its owned resources.
            # Waiting for resource cleanup first can leave an interactive
            # tool waiting for a parent turn that hasn't been cancelled yet.
            if self.turn_task is not None and not self.turn_task.done():
                self.turn_task.cancel()
                cleanup.append(self.turn_task)
            results = await asyncio.gather(*cleanup, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError): raise result
            return True
        if method in {"agents.cancel", "agents.resume_queue"}:
            if not self.engine:
                await self.ensure_engine(write=True)
            self.engine.require_writer()
            if method == "agents.resume_queue":
                return json.loads(await self.engine.available_tools["agent_tool"].execute(action="resume_queue", agent_ids=params.get("agentIds")))
            return json.loads(await self.engine.available_tools["agent_tool"].execute(action="cancel", agent_id=params["agentId"]))
        if method == "agents.tree":
            from xuloom.agents.pipeline import tree, read_tree
            return tree(self.engine.tool_context, params.get("runId")) if self.engine else read_tree(str(self.cwd), self.resume_id, params.get("runId")) if self.resume_id else {"runs": []}
        if method == "agents.pipeline":
            self.require_idle()
            engine = await self.ensure_engine(write=True)
            engine.abort_event.clear()
            self.turn_task = asyncio.create_task(self.pipeline_control(engine, params))
            return {"started": True}
        if method in {"agents.list", "agents.capacity"}:
            await self.ensure_engine()
            return json.loads(await self.engine.available_tools["agent_tool"].execute(action=method.split(".")[1]))
        if method in {"config.save", "config.select", "config.delete", "config.reasoning"}:
            self.require_idle()
            if method == "config.delete":
                if params.get("confirm") is not True:
                    raise ValueError("请确认删除 API 连接")
                change = lambda cfg: delete_connection(cfg, params.get("connectionId"))
            elif method == "config.reasoning":
                change = lambda cfg: select_reasoning(cfg, params)
            else:
                change = (lambda cfg: save_connection(cfg, params)) if method == "config.save" else (lambda cfg: select_model(cfg, params.get("connectionId"), params.get("model")))
            cfg = await asyncio.to_thread(update_config, change)
            provider, network_mode = cfg["model_provider"], cfg["network_mode"]
            from xuloom.security.credentials import is_protected
            if is_protected(active_connection(cfg).get("api_key")):
                # Deleting a connection can select another unavailable credential.
                # Persist the requested edit; the next execution still fails closed.
                for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                    os.environ.pop(name, None)
                return public_config(cfg)
            if method == "config.delete":
                apply_catalog_env(cfg)
            for suffix in ("model", "base_url", "api_key"):
                value = cfg.get(provider + "_" + suffix, "")
                if value:
                    if suffix == "api_key":
                        from xuloom.security.credentials import api_key as checked_key
                        value = checked_key(value)
                    os.environ[provider.upper() + "_" + suffix.upper()] = value
                else:
                    os.environ.pop(provider.upper() + "_" + suffix.upper(), None)
            os.environ["XULOOM_MODEL_PROVIDER"] = provider
            os.environ["XULOOM_NETWORK_MODE"] = network_mode
            if self.engine:
                await self.sync_model(self.engine)
            return public_config(cfg)
        if method == "changes":
            return await git_changes(self.cwd)
        raise ValueError("Unknown method: " + method)

    async def pipeline_control(self, engine, params):
        self.emit({"type": "operation_started", "operation": "pipeline"})
        try:
            receipt = await engine.agents("pipeline", run_id=params.get("runId"), **params.get("spec", {}))
            if not receipt["ok"] or receipt["data"]["runs"][0]["status"] != "completed":
                self.emit({"type": "operation_error", "message": "流水线未完成，请查看运行树及子代理回执；中断操作不会自动重放。"})
        except asyncio.CancelledError:
            self.emit({"type": "operation_error", "message": "流水线已停止，已保存结果保留。"})
        except Exception as error:
            self.emit({"type": "operation_error", "message": str(error)})
        finally:
            self.emit({"type": "operation_finished", "operation": "pipeline"})

    async def media_control(self, engine, action, identity):
        self.emit({"type": "operation_started", "operation": "media", "jobId": identity})
        try:
            engine.activate_tools(["media_tool"])
            receipt = await engine._execute_tool_wrapper({"id": "media-control-" + uuid.uuid4().hex, "name": "media_tool",
                "input": {"action": action, "job_id": identity, "wait_ms": 0}}, lambda _: None)
            from xuloom.media.jobs import load, public
            record = public(load(engine.tool_context, identity))
            self.emit({"type": "media_job_changed", "job": record})
            if receipt["is_error"]:
                self.emit({"type": "operation_error", "message": record.get("error") or "媒体操作未执行；请检查权限与已有任务记录。"})
        except asyncio.CancelledError:
            self.emit({"type": "operation_error", "message": "已停止本机操作；供应商任务可能仍在运行，可继续查询或请求取消。"})
        except Exception as error:
            self.emit({"type": "operation_error", "message": str(error)})
        finally:
            self.emit({"type": "operation_finished", "operation": "media"})

    async def goal_turn(self, engine):
        from xuloom.features import goals
        self.recorder = TurnRecorder()
        engine.turn_traces.append(self.recorder.record)
        engine.active_turn_trace = self.recorder.record
        self.emit({"type": "managed_turn_started", "text": engine.goal["objective"]})
        async def emit(event): self.emit(event)
        try:
            goal = await goals.run(engine, emit)
            result = goal.get("last_result") or {}
            self.emit({"type": "final", "text": result.get("text", ""), "result": result})
            if goal["status"] in {"paused", "cancelled", "interrupted"}:
                self.emit({"type": "turn_cancelled"})
            elif goal["status"] not in {"completed", "awaiting_review"}:
                self.emit({"type": "turn_error", "message": goal["reason"]})
        except asyncio.CancelledError:
            self.emit({"type": "turn_cancelled"})
        except Exception as error:
            self.emit({"type": "turn_error", "message": str(error)})
        finally:
            self.emit({"type": "goal_changed", "goal": copy.deepcopy(engine.goal)})
            self.emit({"type": "turn_finished"})
            engine.active_turn_trace = None
            from xuloom.features.goals import save
            save(engine)
            self.recorder = None

    async def close(self):
        if self.oauth_task and not self.oauth_task.done():
            self.oauth_task.cancel()
            await asyncio.gather(self.oauth_task,return_exceptions=True)
        try:
            await self.dispatch("stop", {})
        finally:
            self.protocol_shutdown.set()
            try:
                if self.protocol_task:
                    await self.mcp_requests.put(None)
                    if self.protocol_ready is not None and not self.protocol_ready.done():
                        self.protocol_task.cancel()
                    results = await asyncio.gather(self.protocol_task, return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError): raise result
            finally:
                if self.engine:
                    await self.engine.aclose()


async def serve(bridge_factory=DesktopBridge):
    wire = sys.stdout
    sys.stdout = sys.stderr
    def emit(payload):
        wire.write(json.dumps({"event": payload}, ensure_ascii=False, default=str) + "\n")
        wire.flush()
    bridge = bridge_factory(emit)
    requests = set()
    stdin_fd = sys.stdin.fileno()
    read_chunk = pipe_reader(stdin_fd)
    buffered = b""
    async def handle(request):
        try:
            result = await bridge.dispatch(request["method"], request.get("params") or {})
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            from xuloom.core.session_lock import SessionBusyError, SessionConflictError
            from xuloom.core.run_result import PersistenceError
            code = "session_busy" if isinstance(exc, SessionBusyError) else "session_conflict" if isinstance(exc, SessionConflictError) else "persistence_error" if isinstance(exc, PersistenceError) else "invalid_request" if isinstance(exc, ValueError) else "error"
            if getattr(exc, "code", None) in {"session_busy", "session_conflict", "persistence_error", "invalid_request"}: code = exc.code
            response = {"id": request.get("id"), "error": str(exc), "errorCode": code}
        wire.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        wire.flush()
    try:
        while True:
            # TextIOWrapper.readline holds its buffer lock while idle. Library
            # imports checking stdin.isatty() would then deadlock the agent loop.
            chunk = await asyncio.to_thread(read_chunk)
            if not chunk:
                break
            buffered += chunk
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                try:
                    request = json.loads(line.decode("utf-8"))
                    if not isinstance(request, dict) or "id" not in request or not isinstance(request.get("method"), str):
                        raise ValueError("Invalid RPC request")
                    task = asyncio.create_task(handle(request))
                    requests.add(task)
                    task.add_done_callback(requests.discard)
                except (ValueError, UnicodeError) as exc:
                    emit({"type": "protocol_error", "message": str(exc)})
    finally:
        await bridge.close()
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    asyncio.run(serve())
