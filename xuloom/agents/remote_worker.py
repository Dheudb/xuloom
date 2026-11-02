"""A transport handle owned by the native worker queue, not another scheduler."""
import asyncio
import copy
import time
import uuid

from xuloom.agents.executors import profile
from xuloom.core.output_limits import active_budgets, reserve, settle, ModelBudgetError
from xuloom.core.run_result import PersistenceError
from xuloom.remote import worker_protocol as wire
from xuloom.remote.config import target
from xuloom.remote.ssh import SSHClient, RemoteDisconnected
from xuloom.security.delegation import authorize_delegated, request_from_wire
from xuloom.security.permissions import PermissionDecision


def prepare(context, definition, row, reference=None):
    host = profile(row["executor"], context)
    role = {key: copy.deepcopy(value) for key, value in definition.items() if key in wire.ROLE_FIELDS}
    value = {"owner": {"token": uuid.uuid4().hex, "parent_session_id": context["session_id"],
        "parent_agent_id": row["parent_agent_id"], "agent_id": row["agent_id"], "depth": row["agent_depth"]},
        "role": wire.freeze_role(role), "context": reference}
    return {"profile": target(host), "open": wire.open_request(value), "run_id": str(uuid.uuid4())}


class RemoteWorker:
    def __init__(self, tool, identity):
        self.tool, self.identity = tool, identity
        self.client = None
        self.done = None
        self.run_id = None
        self.handlers, self.leases = set(), {}
        self.budgets = []
        self.info = None
        self.active = False
        self.opened = False
        self.proven_closed = False
        self.submitted = False
        self.receipt_bound = False

    def row(self):
        return self.tool._worker(self.identity)

    def specification(self):
        row = self.row(); spec = row["dispatch"]["remote"]
        profile(row["executor"], self.tool.context, frozen=spec["profile"])
        if spec["open"]["owner"]["parent_session_id"] != self.tool.context["session_id"] or spec["open"]["owner"]["agent_id"] != self.identity:
            raise ValueError("Remote worker does not belong to this native conversation")
        return spec

    def _bind_receipt(self):
        """Restore the local checkpoint once under its parent writer lease."""
        manager = self.tool.context.get('session_manager')
        if manager is None:
            raise PersistenceError('Remote worker receipt storage is unavailable')
        manager.acquire_writer(self.tool.context['session_id'])
        if self.receipt_bound:
            manager.check_subagent_current(self.identity)
            return
        try:
            saved = manager.load_subagent_transcript(self.identity)
        except FileNotFoundError:
            if self.row()['dispatch'].get('resume'):
                raise PersistenceError('The previous remote worker receipt is missing; no continuation was started') from None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise PersistenceError('The remote worker receipt could not be loaded; existing records were preserved') from error
        else:
            if not isinstance(saved, dict) or saved.get('session_id') != self.tool.context['session_id'] or saved.get('agent_id') != self.identity:
                raise PersistenceError('The remote worker receipt belongs to a different owner; existing records were preserved')
            reference = saved.get('remote_reference')
            if reference is not None and (not isinstance(reference, dict) or
                    reference.get('session_id') != wire.session_id(self.specification()['open']['owner'])):
                raise PersistenceError('The remote worker receipt identity changed; existing records were preserved')
        self.receipt_bound = True

    def snapshot(self, value, *, current=True):
        spec = self.specification()
        if not isinstance(value, dict) or value.get("session_id") != wire.session_id(spec["open"]["owner"]) or value.get("agent_id") != self.identity:
            raise ValueError("Remote response does not match the owned worker")
        run = value.get("run")
        if run is not None and (not isinstance(run, dict) or current and run.get("id") != self.run_id):
            raise ValueError("Remote response does not match the owned execution")
        return value

    def _event(self, event):
        kind = event.get("type")
        if kind == "worker_budget_settled":
            entry = self.leases.pop(event.get("leaseId"), None)
            if entry is not None:
                reported = event.get("reported")
                settle(entry[0], entry[1], reported if type(reported) is int and reported >= 0 else 0)
        elif kind == "worker_messages_consumed":
            try:
                self.tool._ack_mailbox(self.identity, set(event["messageIds"]))
            except OSError:
                pass  # Keep the parent inbox; an explicit status check can reconcile it.
        elif kind in {"worker_permission", "worker_budget"}:
            task = asyncio.create_task(self._respond(event)); self.handlers.add(task); task.add_done_callback(self.handlers.discard)
        elif kind in {"worker_finished", "remote_disconnected"} and self.done is not None and not self.done.done():
            if kind == "remote_disconnected":
                self.done.set_exception(RemoteDisconnected("Worker transport ended; remote outcome is unconfirmed"))
            elif event.get("runId") == self.run_id:
                self.done.set_result(event["result"])

    async def _respond(self, event):
        answer = {"behavior": "deny", "remember": None, "approval_revision": 0, "reason": "Native parent authority is unavailable"}
        try:
            if not self.active or self.done is None or self.done.done():
                raise ValueError("No active owned worker request")
            if event["type"] == "worker_permission":
                automatic = PermissionDecision(**event["automatic"]) if event.get("automatic") is not None else None
                answer = await authorize_delegated(self.tool.context.get("permission_manager"), request_from_wire(event["request"]),
                    automatic, event.get("aliases"), remote={"host": self.specification()["profile"]["host"], "cwd": self.info["cwd"]},
                    grant_revision=event.get("grantRevision"))
            else:
                maximum = event.get("maximum")
                if type(maximum) is not int or maximum <= 0:
                    raise ValueError("Invalid remote model reservation")
                cap = self.tool.context["collaboration_run"]["request_cap"]
                amount = reserve(min(maximum, cap), self.budgets)
                identity = str(uuid.uuid4()); self.leases[identity] = (self.budgets, amount)
                # Checkpoint the existing parent/goal counters before a host
                # can spend the grant; recovery already charges unknown leases.
                await self.tool._write_subagent_registry_record({"agent_id": self.identity})
                answer = {"ok": True, "id": identity, "maximum": amount}
        except Exception as error:
            if event["type"] == "worker_budget":
                answer = {"ok": False, "reason": "budget_exhausted" if isinstance(error, ModelBudgetError) else "grant_unavailable"}
        try:
            await self.client.request("respond", {"requestId": event["requestId"], "answer": answer})
        except (ConnectionError, OSError, ValueError):
            pass  # No replay. A disconnected host cannot receive this grant.

    async def connect(self):
        self._bind_receipt()
        spec = self.specification()
        if self.client is None or self.client.closed:
            if self.client is not None:
                await self.client.aclose(graceful=False)
            self.client = SSHClient(spec["profile"], self._event, worker=True)
            self.opened = False
            self.proven_closed = False
            self.info = await self.client.start()
            if self.info.get("worker_protocol") != wire.VERSION:
                raise ValueError("Update the execution host: its native worker protocol is incompatible")
        value = self.snapshot(await self.client.request("worker.open", spec["open"]), current=False)
        self.opened = True
        return value

    async def deliver(self):
        incoming = self.tool._mailbox(self.identity)
        if incoming:
            answer = await self.client.request("worker.messages", {"messages": incoming})
            if not isinstance(answer, dict) or set(answer.get("accepted", [])) != {row["id"] for row in incoming}:
                raise ValueError("Remote message acknowledgement does not match the native mailbox")
            self.tool._ack_mailbox(self.identity, set(answer["consumed"]))
            return answer

    async def refresh(self):
        try:
            snapshot = await self.connect()
            pending = self.tool._mailbox(self.identity)
            if pending:
                state = await self.client.request("worker.message_status", {"message_ids": [row["id"] for row in pending]})
                self.tool._ack_mailbox(self.identity, set(state["consumed"]))
            return await self.finish(snapshot)
        except (ConnectionError, OSError, ValueError) as error:
            if self.row().get("status") == "completed":
                # A saved successful result remains valid when its host is offline.
                return self.row()
            return await self.finish(unknown=True, text=str(error))

    async def stop(self):
        if self.proven_closed:
            return {"stopped": True, "outcome_unknown": False}
        if self.client is None or self.client.closed:
            await self.connect()
        stopped = await self.client.request("stop", timeout=25)
        if stopped.get("stopped"):
            return stopped
        response = await self.client.request("worker.cancel", {"run_id": self.run_id})
        deadline = asyncio.get_running_loop().time() + 25
        while response.get("requested") and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.5)
            snapshot = self.snapshot(await self.client.request("worker.status", timeout=5))
            if snapshot["cleanup_confirmed"]:
                return {"stopped": True, "outcome_unknown": False, "worker": snapshot}
        return stopped

    async def close(self):
        if self.proven_closed:
            return
        try:
            if self.client is not None:
                response = await self.stop() if self.opened else {"stopped": True}
                await self.client.aclose(graceful=not self.client.closed)
                if not response.get("stopped"):
                    raise RemoteDisconnected("Remote worker cleanup is unconfirmed; inspect its saved execution before continuing")
            self.proven_closed = True
        finally:
            for task in list(self.handlers): task.cancel()
            await asyncio.gather(*self.handlers, return_exceptions=True)
            self._settle_unknown()

    def _settle_unknown(self):
        for budgets, amount in self.leases.values(): settle(budgets, amount, 0)
        self.leases.clear()

    async def finish(self, snapshot=None, *, unknown=False, text="", cancelled=False):
        self._bind_receipt()
        row = self.row()
        if snapshot is not None:
            snapshot = self.snapshot(snapshot)
            run = snapshot.get("run") or {}
            receipt = copy.deepcopy(run.get("receipt") or {})
            status = snapshot["phase"] if snapshot["phase"] in {"completed", "cancelled", "crashed"} else "interrupted"
            unknown = unknown or status == "interrupted"
        else:
            receipt, status = {}, "interrupted" if unknown else "cancelled" if cancelled else "crashed"
        if status == "completed" and (receipt.get("ok") is not True or receipt.get("persisted") is not True):
            status = "crashed"
        result = str(receipt.get("text") or text or ("Remote outcome is unconfirmed; inspect its saved history before any continuation" if unknown else "Remote execution did not complete"))
        payload = {"agent_id": self.identity, "agent_role": row["agent_role"], "task": row["dispatch"]["task"],
            "status": status, "phase": status, "result": result, "receipt": receipt, "outcome_unknown": unknown}
        manager = self.tool.context.get("session_manager")
        if manager is None or not manager.save_subagent_transcript(self.identity, {**payload,
                "session_id": self.tool.context["session_id"], "messages": [], "remote_reference": row.get("remote_reference"),
                "metadata": {"delegation": {"owner_session_id": self.tool.context["session_id"]}}}):
            payload.update(status="crashed", phase="crashed", receipt={"ok": False, "persisted": False, "reason": "persistence_error"})
        await self.tool._write_subagent_registry_record({**payload, "finished_at": int(time.time() * 1000),
            "running_commands": 0 if snapshot and snapshot.get("cleanup_confirmed") else snapshot.get("running_commands") if snapshot else None})
        return payload

    async def collected(self, snapshot):
        payload = await self.finish(snapshot)
        if snapshot.get("running_commands") == 0 and snapshot.get("phase") in {"completed", "cancelled", "crashed"}:
            try:
                await self.close()
            except (ConnectionError, OSError, ValueError):
                payload["outcome_unknown"] = True
                await self.tool._write_subagent_registry_record({"agent_id": self.identity, "outcome_unknown": True})
        return payload

    async def run(self):
        spec = self.specification()
        self.run_id = spec["run_id"]
        self.done = asyncio.get_running_loop().create_future()
        self.active = True
        self.submitted = False
        self.budgets = active_budgets(self.tool.context.get("goal_budget"), self.tool.context["collaboration_run"])
        submitted = False
        try:
            opened = await self.connect()
            from xuloom.features.chat_links import remote_chat_link
            binding = opened["model_binding"]
            reference = {"url": remote_chat_link(spec["profile"], opened["session_id"]), "session_id": opened["session_id"],
                "host": spec["profile"]["host"], "cwd": self.info["cwd"], "machine": self.info["hostname"], "connection_id": spec["profile"]["id"]}
            await self.tool._write_subagent_registry_record({"agent_id": self.identity, "remote_reference": reference,
                "model": binding["model"], "provider": binding["provider"], "api_base_url": binding["api_base_url"], "phase": "running"})
            await self.deliver()
            submitted = True  # The request may execute even when its reply is lost.
            self.submitted = True
            reply = await self.client.request("worker.start", {"run_id": self.run_id, "text": self.row()["dispatch"]["task"]})
            if reply.get("reused"):
                snapshot = self.snapshot(reply)
                if snapshot["phase"] != "running" or not snapshot["connected_execution"]:
                    return await self.collected(snapshot)
            return await self.collected(await self.done)
        except asyncio.CancelledError:
            if not submitted:
                return await self.finish(cancelled=True, text="Cancelled before remote execution")
            try:
                stopped = await self.stop()
                if stopped.get("stopped") and stopped.get("worker"):
                    return await self.finish(stopped["worker"])
            except (ConnectionError, ValueError, OSError):
                pass
            return await self.finish(unknown=submitted, text="Remote stop could not be confirmed" if submitted else "Cancelled before remote execution")
        except (ConnectionError, OSError, ValueError) as error:
            return await self.finish(unknown=submitted, text=str(error))
        finally:
            self.active = False
            for task in list(self.handlers): task.cancel()
            await asyncio.gather(*self.handlers, return_exceptions=True)
            self._settle_unknown()
            if self.done.done() and not self.done.cancelled(): self.done.exception()
