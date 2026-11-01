"""A single owned native execution host over the existing SSH JSONL transport."""
import asyncio
import copy
import dataclasses
import json
import os
from pathlib import Path
import sys

from xuloom.agents.connections import resolve_connection
from xuloom.agents.role_runtime import configure_role, role_prompt
from xuloom.core.config import load_config
from xuloom.core.model_catalog import active_connection
from xuloom.core.run_result import PersistenceError
from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionBusyError, SessionLock
from xuloom.core.storage import artifact_directory, atomic_json
from xuloom.desktop.bridge import DesktopBridge, serve
from xuloom.remote import worker_protocol as protocol
from xuloom.security.permissions import PermissionDecision


class WorkerBridge(DesktopBridge):
    owned_worker_controller = True

    def __init__(self, emit, cwd=None):
        super().__init__(emit, cwd)
        self.spec = self.claim = None
        self.opening = asyncio.Lock()
        self.starting = asyncio.Lock()
        self.grant_revision = None
        self.required_runtime_error = None
        self.control_task = None
        self.worker_closed = False
        self.approval_policy.set_mode("full")
        self.collaboration_mode = "solo"

    def _read(self, manager, claim=None, role=None):
        claim = self.claim if claim is None else claim
        path = Path(manager.session_dir) / (claim["session_id"] + ".json")
        from xuloom.features.chat_links import _read
        from xuloom.core.storage import data_directory
        _read(path, data_directory() / "history")
        state = manager.load_session_state(claim["session_id"])
        if not state.get("_valid"):
            raise ValueError("Remote worker checkpoint is invalid; it was not overwritten")
        delegated = state["metadata"].get("delegation")
        saved = delegated.get("remote_worker") if isinstance(delegated, dict) else None
        if not isinstance(saved, dict):
            raise ValueError("Remote worker control checkpoint is invalid")
        if any(saved.get(key) != value for key, value in claim.items()):
            raise ValueError("Remote worker ownership or frozen role does not match")
        if delegated.get("authority_required") is not True:
            raise ValueError("Remote worker authority marker is invalid")
        expected_role = self.spec["role"] if role is None else role
        if saved.get("role") != expected_role or not isinstance(saved.get("system_prompt"), str) or not saved["system_prompt"].strip():
            raise ValueError("Remote worker frozen instructions are invalid or changed")
        model = saved.get("model_binding")
        if not isinstance(model, dict) or set(model) != {"connection_id", "provider", "model", "api_base_url", "network_mode", "contract"}:
            raise ValueError("Remote worker model binding is invalid")
        if not isinstance(saved.get("mailbox"), list) or not isinstance(saved.get("received_messages"), dict):
            raise ValueError("Remote worker mailbox checkpoint is invalid")
        run = saved.get("run")
        if run is not None and (not isinstance(run, dict) or set(run) != {"id", "input_digest", "phase", "receipt"} or
                not isinstance(run["id"], str) or not protocol.IDENTITY.fullmatch(run["id"]) or
                not isinstance(run["input_digest"], str) or len(run["input_digest"]) != 64 or
                run["phase"] not in {"running", "completed", "cancelled", "crashed"} or
                run["receipt"] is not None and not isinstance(run["receipt"], dict) or
                run["phase"] == "completed" and (not run["receipt"] or run["receipt"].get("ok") is not True or run["receipt"].get("persisted") is not True)):
            raise ValueError("Remote worker execution receipt is invalid")
        return state, saved

    async def _open(self, params):
        spec, claim = protocol.open_request(params), protocol.binding(params)
        async with self.opening:
            if self.claim is not None and self.claim != claim:
                raise ValueError("This SSH channel already belongs to another worker or role")
            with SessionManager(cwd=str(self.cwd)) as manager:
                path = Path(manager.session_dir) / (claim["session_id"] + ".json")
                if os.path.lexists(path):
                    self._read(manager, claim, spec["role"])
                else:
                    manager.acquire_writer(claim["session_id"])
                    if os.path.lexists(path):
                        self._read(manager, claim, spec["role"])
                    else:
                        role = spec["role"]
                        identity = role.get("connection") or active_connection(load_config())["id"]
                        model, _ = resolve_connection(identity, role.get("model"))
                        frozen = role_prompt(role, self.skills, spec["context"])
                        record = {**claim, "role": role, "model_binding": model, "system_prompt": frozen, "run": None,
                            "mailbox": [], "received_messages": {}, "cleanup": None}
                        marker = artifact_directory(str(self.cwd)) / claim["session_id"] / "worker-identity.json"
                        if marker.exists():
                            raise PersistenceError("Owned worker checkpoint is missing; it was not recreated")
                        atomic_json(marker, claim)
                        metadata = {"delegation": {"authority_required": True, "remote_worker": record},
                            "collaboration_mode": "solo", "agent": {"agent_id": claim["agent_id"]}}
                        if not manager.save_session_state(claim["session_id"], [], metadata):
                            raise PersistenceError("Could not save remote worker identity; no model was started")
            self.spec, self.claim, self.resume_id = spec, claim, claim["session_id"]
        return self._snapshot()

    def _snapshot(self):
        if self.claim is None:
            raise ValueError("Open the owned worker before requesting its status")
        with SessionManager(cwd=str(self.cwd)) as manager:
            _, saved = self._read(manager)
            owner = bool(self.engine and self.engine.session_manager.owns_writer(self.resume_id))
            if not owner:
                lock = SessionLock(manager.lock_directory, self.resume_id)
                try:
                    lock.acquire()
                except SessionBusyError:
                    owner = True
                finally:
                    lock.close()
        run = copy.deepcopy(saved["run"])
        phase = run["phase"] if run else "ready"
        if phase == "running" and not self.busy():
            phase = "owner_busy" if owner else "interrupted"
        return {"session_id": self.resume_id, "agent_id": self.claim["agent_id"], "model_binding": saved["model_binding"],
            "phase": phase, "owner_present": owner, "connected_execution": phase == "running" and self.busy(), "run": run,
            "cleanup_confirmed": isinstance(saved.get("cleanup"), dict) and saved["cleanup"].get("run_id") == (run or {}).get("id") and saved["cleanup"].get("confirmed") is True,
            "running_commands": sum(row.get("status") == "running" for row in self.engine.tool_context["command_sessions"].records.values()) +
                sum(row["status"] == "running" for row in self.engine.shell_task_manager.list_tasks()) if self.engine else None}

    def sessions(self):
        # DesktopBridge emits a history refresh after each turn. This endpoint
        # is scoped to one worker, not the host's unrelated conversations.
        if self.claim is None:
            return []
        with SessionManager(cwd=str(self.cwd)) as manager:
            state, _ = self._read(manager)
            history = self.history(state["messages"])
            path = Path(manager.session_dir) / (self.resume_id + ".json")
            return [{"id": self.resume_id, "title": next((row["text"][:65] for row in history if row["role"] == "user"), "Remote worker"),
                "updated": path.stat().st_mtime}]

    def _saved(self):
        if self.engine is not None:
            return self.engine.tool_context["delegation"]["remote_worker"]
        with SessionManager(cwd=str(self.cwd)) as manager:
            return self._read(manager)[1]

    def _create_engine(self):
        from xuloom.core.engine import QueryEngine
        saved = self._saved()
        frozen = saved["model_binding"]
        model, options = resolve_connection(frozen["connection_id"], frozen["model"], frozen=frozen)
        return QueryEngine(cwd=str(self.cwd), agent_id=self.claim["agent_id"], parent_agent_id=self.claim["parent_agent_id"],
            agent_role=self.spec["role"].get("agent_role", "remote-worker"), collaboration_mode="solo", execution_mode="host",
            permission_handler=self.permission, approval_policy=self.approval_policy, model=model["model"], fallback_model=model["model"],
            model_provider=model["provider"], api_base_url=model["api_base_url"], model_contract=model["contract"],
            reasoning_effort=self.spec["role"].get("reasoning_effort"), **options)

    async def sync_model(self, engine, force=False):
        frozen = self._saved()["model_binding"]
        _, options = resolve_connection(frozen["connection_id"], frozen["model"], frozen=frozen)
        if engine._api_key != options["api_key"]:
            raise ValueError("Remote key changed; reconnect the same worker before continuing")

    def _configure_engine(self, engine):
        saved = engine.tool_context["delegation"]["remote_worker"]
        configure_role(engine, saved["role"], allowed_tools=saved["role"].get("allowed_tools"), skills=self.skills,
            frozen_prompt=saved["system_prompt"])
        engine.permission_manager.delegated_authorizer = self._authorize
        if self.control_task is None or self.control_task.done():
            self.control_task = asyncio.create_task(self._watch_control())
        engine.tool_context.update(acquire_model_budget=self._reserve_model,
            settle_model_budget=lambda lease, reported: self.output({"type": "worker_budget_settled", "leaseId": lease["id"], "reported": reported}),
            delegated_mailbox=lambda: copy.deepcopy(self._saved()["mailbox"]), ack_delegated_mailbox=self._ack_messages)
        if self.protocol_ready is not None and self.protocol_ready.done():
            if self.required_runtime_error:
                raise ValueError(self.required_runtime_error)
            allowed = saved["role"].get("allowed_tools")
            if allowed is not None and "*" not in allowed:
                missing = set(allowed) - engine.available_tools.keys() - engine.latent_tools.keys()
                if missing:
                    raise ValueError("Remote role requires unavailable or disallowed tools: " + ", ".join(sorted(missing)))

    async def _connect_mcp(self, engine):
        names = self.spec["role"].get("mcp_servers")
        if names is None:
            return await super()._connect_mcp(engine)
        self.required_runtime_error = None
        result = {}
        try:
            for name in names:
                result.update(await engine.mcp_bridge.load_and_connect(only=name))
                if name not in engine.mcp_bridge.sessions:
                    raise ValueError("Remote role requires an unavailable MCP server: " + name)
        except Exception:
            self.required_runtime_error = "Remote role's required MCP services could not connect; no model was started"
            raise
        return result

    async def _authorize(self, request, automatic, aliases):
        answer = await self.ask("worker_permission", {"request": dataclasses.asdict(request),
            "automatic": dataclasses.asdict(automatic) if automatic is not None else None, "aliases": aliases,
            "grantRevision": self.grant_revision})
        if not isinstance(answer, dict) or answer.get("behavior") not in {"allow", "deny"} or type(answer.get("approval_revision")) is not int or answer.get("remember") not in {None, "allow", "deny"}:
            raise ValueError("Invalid parent authority response")
        manager = self.engine.permission_manager
        if answer["approval_revision"] != self.grant_revision:
            manager.always_allow_tools.clear()
            self.grant_revision = answer["approval_revision"]
        if answer.get("remember") == "allow" and answer["behavior"] == "allow":
            manager.always_allow_tools.add(request.tool_name)
        if answer.get("remember") == "deny" and answer["behavior"] == "deny":
            manager.always_deny_tools.add(request.tool_name)
        return PermissionDecision(answer["behavior"], str(answer.get("reason", "Parent authority decision")), source="delegated_authority")

    async def _reserve_model(self, maximum):
        answer = await self.ask("worker_budget", {"maximum": maximum})
        if not isinstance(answer, dict) or not answer.get("ok"):
            if isinstance(answer, dict) and answer.get("reason") == "budget_exhausted":
                from xuloom.core.output_limits import ModelBudgetError
                raise ModelBudgetError("Delegating model output budget is exhausted; no model was started")
            raise RuntimeError("Delegating model budget is unavailable or exhausted; no model was started")
        return {"id": answer.get("id"), "maximum": answer.get("maximum")}

    def _ack_messages(self, identities):
        saved = self._saved(); previous = saved["mailbox"]
        saved["mailbox"] = [row for row in previous if row["id"] not in identities]
        if not self.engine.persist_session_state():
            saved["mailbox"] = previous
            raise PersistenceError("Could not save worker message consumption")
        self.output({"type": "worker_messages_consumed", "messageIds": sorted(identities)})

    def _control_path(self):
        return artifact_directory(str(self.cwd)) / self.resume_id / "worker-control.json"

    def _control_lock(self):
        return SessionLock(artifact_directory(str(self.cwd)).parent / "worker-controls", self.resume_id)

    async def _watch_control(self):
        # A file control inbox, like goal controls; no model polling or daemon.
        while not self.protocol_shutdown.is_set():
            await asyncio.sleep(.25)
            path = self._control_path()
            if not path.exists():
                continue
            lock = self._control_lock()
            try:
                lock.acquire()
                value = json.loads(path.read_text(encoding="utf-8"))
                expected = {"owner_digest": self.claim["owner_digest"], "run_id": (self._saved()["run"] or {}).get("id"), "action": "cancel"}
                if value == expected:
                    await self.dispatch("stop", {})
                if path.exists() and json.loads(path.read_text(encoding="utf-8")) == value:
                    path.unlink()
            except (OSError, ValueError):
                self.output({"type": "worker_control_error", "message": "Worker control could not be confirmed; inspect its saved state"})
            finally:
                lock.close()

    async def close(self):
        if self.worker_closed:
            return
        try:
            await super().close()
            self.worker_closed = True
        finally:
            if self.control_task is not None and self.control_task is not asyncio.current_task():
                self.control_task.cancel()
                await asyncio.gather(self.control_task, return_exceptions=True)

    async def _execute(self, text, run_id):
        result = None
        try:
            result = await self.turn(text, [], "normal")
        except asyncio.CancelledError:
            result = {"ok": False, "reason": "cancelled", "status": "cancelled"}
        except Exception as error:
            result = {"ok": False, "reason": "execution_error", "status": "crashed", "text": str(error)}
        finally:
            result = result or {"ok": False, "reason": "execution_error", "status": "crashed"}
            if result.get("ok") and not str(result.get("text") or "").strip():
                result.update(ok=False, reason="empty_result", status="crashed")
            phase = "completed" if result.get("ok") and result.get("persisted") else "cancelled" if result.get("reason") == "cancelled" else "crashed"
            control = self._saved()["run"]
            if control["id"] != run_id:
                raise ValueError("Remote execution ownership changed")
            control.update(phase=phase, receipt=result)
            if not self.engine.persist_session_state():
                self.output({"type": "worker_finished", "runId": run_id, "result": {"ok": False, "reason": "persistence_error", "persisted": False}})
            else:
                self.output({"type": "worker_finished", "runId": run_id, "result": self._snapshot()})

    async def dispatch(self, method, params):
        if method == "runtime.info":
            return {**await super().dispatch(method, params), "worker_protocol": protocol.VERSION}
        if method == "worker.open":
            return await self._open(params)
        if method == "stop":
            async with self.starting:
                if self.claim is not None and self.engine is None:
                    try:
                        snapshot = self._snapshot()
                    except (OSError, ValueError):
                        return {"stopped": False, "outcome_unknown": True, "reason": "checkpoint_unreadable"}
                    confirmed = snapshot["phase"] == "ready" or snapshot["cleanup_confirmed"]
                    return {"stopped": confirmed, "outcome_unknown": not confirmed, "worker": snapshot}
                await super().dispatch(method, params)
                if self.engine is not None:
                    self._saved()["cleanup"] = {"run_id": (self._saved()["run"] or {}).get("id"), "confirmed": True}
                    if not self.engine.persist_session_state():
                        raise PersistenceError("Worker stopped, but its cleanup confirmation could not be saved")
                return {"stopped": True, "outcome_unknown": False, "worker": self._snapshot() if self.claim else None}
        if method == "shutdown":
            return await super().dispatch(method, params)
        if self.claim is None:
            raise ValueError("This endpoint requires an owned worker; generic session operations are unavailable")
        if method == "worker.status":
            return self._snapshot()
        if method == "worker.message_status":
            identities = params.get("message_ids") if isinstance(params, dict) else None
            if not isinstance(identities, list) or not all(isinstance(identity, str) and protocol.IDENTITY.fullmatch(identity) for identity in identities):
                raise ValueError("Supply worker message IDs")
            saved = self._saved()
            pending = {row["id"] for row in saved["mailbox"]}
            return {"consumed": [identity for identity in identities if identity in saved["received_messages"] and identity not in pending]}
        if method == "worker.cancel":
            if not isinstance(params, dict) or set(params) != {"run_id"} or not isinstance(params["run_id"], str):
                raise ValueError("Supply the owned remote run ID")
            current = self._snapshot()
            if (current.get("run") or {}).get("id") != params["run_id"]:
                raise ValueError("Remote cancel does not match the saved execution")
            if current["cleanup_confirmed"]:
                return {"requested": False, "stopped": True, "outcome_unknown": False, "worker": current}
            lock = self._control_lock(); lock.acquire()
            try:
                atomic_json(self._control_path(), {"owner_digest": self.claim["owner_digest"], "run_id": params["run_id"], "action": "cancel"})
            finally:
                lock.close()
            return {"requested": True, "stopped": False, "outcome_unknown": True, "worker": current}
        if method == "respond":
            return await super().dispatch(method, params)
        if method == "worker.messages":
            if not isinstance(params, dict) or set(params) != {"messages"} or not isinstance(params["messages"], list):
                raise ValueError("Supply the worker's message list")
            rows = params["messages"]
            fingerprints = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"id", "text", "at"} or not isinstance(row["id"], str) or not protocol.IDENTITY.fullmatch(row["id"]) or not isinstance(row["text"], str) or not row["text"].strip() or type(row["at"]) is not int:
                    raise ValueError("Invalid worker message")
                if row["id"] in fingerprints and fingerprints[row["id"]] != protocol.digest(row):
                    raise ValueError("Worker message ID already has different content")
                fingerprints[row["id"]] = protocol.digest(row)
            engine = await self.ensure_engine(write=True)
            saved = self._saved(); previous = copy.deepcopy((saved["mailbox"], saved["received_messages"]))
            for row in rows:
                key, fingerprint = row["id"], protocol.digest(row)
                if key in saved["received_messages"] and saved["received_messages"][key] != fingerprint:
                    raise ValueError("Worker message ID already has different content")
            new = {row["id"]: row for row in rows if row["id"] not in saved["received_messages"]}
            if len(saved["mailbox"]) + len(new) > 100:
                raise ValueError("Worker mailbox is full")
            saved["mailbox"].extend(copy.deepcopy(list(new.values())))
            saved["received_messages"].update({key: protocol.digest(row) for key, row in new.items()})
            if not engine.persist_session_state():
                saved["mailbox"], saved["received_messages"] = previous
                raise PersistenceError("Could not save worker messages; no acknowledgement was sent")
            pending = {row["id"] for row in saved["mailbox"]}
            return {"accepted": [row["id"] for row in rows], "consumed": [row["id"] for row in rows if row["id"] not in pending], "running": self.busy()}
        if method == "worker.start":
            if not isinstance(params, dict) or set(params) != {"run_id", "text"} or not isinstance(params["run_id"], str) or not protocol.IDENTITY.fullmatch(params["run_id"]) or not isinstance(params["text"], str) or not params["text"].strip():
                raise ValueError("Supply a real run ID and non-empty remote assignment")
            async with self.starting:
                saved = self._saved()
                if saved["run"] and saved["run"]["id"] == params["run_id"]:
                    if saved["run"]["input_digest"] != protocol.digest(params["text"]):
                        raise ValueError("Remote run ID already has a different assignment")
                    return {"accepted": True, "reused": True, **self._snapshot()}
                self.require_idle()
                engine = await self.ensure_engine(write=True)
                self.require_idle()
                saved = self._saved()
                previous_run, previous_cleanup = copy.deepcopy(saved["run"]), copy.deepcopy(saved["cleanup"])
                saved["run"] = {"id": params["run_id"], "input_digest": protocol.digest(params["text"]), "phase": "running", "receipt": None}
                saved["cleanup"] = None
                if not engine.persist_session_state():
                    saved["run"] = previous_run
                    saved["cleanup"] = previous_cleanup
                    raise PersistenceError("Could not save remote execution intent; no model was started")
                self.stop_requested = False
                self.turn_task = asyncio.create_task(self._execute(params["text"], params["run_id"]))
                return {"accepted": True, "session_id": self.resume_id, "run_id": params["run_id"]}
        raise ValueError("This worker endpoint does not expose " + method)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    asyncio.run(serve(WorkerBridge))


if __name__ == "__main__":
    main()
