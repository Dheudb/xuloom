"""Session-owned, resumable workers. Host liveness is distinct from saved output."""
import asyncio
import copy
import json
import re
import time
import uuid
from pathlib import Path


def concurrency_limit(context):
    from xuloom.agents.collaboration import policy
    registry = context.get("agent_registry")
    profile = context.get("collaboration")
    if profile is None:
        profile = policy("auto", registry.skills.settings() if registry else {})
    return profile["concurrency"]



class AgentLifecycle:
    def _worker(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise ValueError("A valid agent_id is required")
        row = next((r for r in self.context.get("subagent_registry", []) if r.get("agent_id") == identity), None)
        if row is None:
            raise ValueError("Unknown subagent in this conversation")
        return row

    def _inventory(self):
        tasks = self.context["subagent_tasks"]
        rows = []
        for saved in self.context.get("subagent_registry", []):
            row = copy.deepcopy(saved)
            task = tasks.tasks.get(row["agent_id"])
            row["pending"] = bool(task and not task.done())
            row["executing"] = row["agent_id"] in tasks.running
            if row.get("status") == "running" and not row["pending"]:
                row["status"] = "interrupted"
            if row.get("status") == "queued" and not row["pending"]:
                row["phase"] = "awaiting_resume"
            row["pending_message_count"] = len(row.pop("pending_messages", []))
            commands = self.context.get("command_sessions")
            row["running_commands"] = sum(r["owner"] == row["agent_id"] and r["status"] == "running" for r in commands.records.values()) if commands else 0
            row.pop("role_definition", None)
            row.pop("dispatch", None)
            row.pop("api_base_url", None)
            binding = row.pop("connection_binding", None)
            if binding:
                row["connection_id"] = binding["connection_id"]
            rows.append(row)
        return rows

    def _check_worker_connection(self, row):
        binding = row.get("connection_binding")
        if binding is not None:
            from xuloom.agents.connections import resolve_connection
            resolve_connection(binding["connection_id"], binding["model"], frozen=binding)
            if row.get("provider") != binding["provider"] or (row.get("api_base_url") or "") != binding["api_base_url"] or row.get("model") != binding["model"]:
                raise ValueError("Worker record does not match its frozen connection")
            return binding["provider"], binding["api_base_url"]
        if row.get("provider") != self.context.get("model_provider") or (row.get("api_base_url") or "") != (self.context.get("api_base_url") or ""):
            raise ValueError("Restore the worker's original provider and API address before continuing")
        return self.context.get("model_provider"), self.context.get("api_base_url")

    def _resume_state(self, row):
        provider, base = self._check_worker_connection(row)
        manager = self.context.get("session_manager")
        if manager is None:
            raise ValueError("Subagent transcript storage is unavailable")
        path = Path(manager.get_subagent_transcript_path(row["agent_id"]))
        raw = manager.load_subagent_transcript(row["agent_id"])
        if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
            raise ValueError("Invalid subagent transcript")
        # Resolve from our own central storage, never a model-supplied path.
        owner = raw.get("session_id") or raw.get("metadata", {}).get("delegation", {}).get("owner_session_id")
        if owner and owner != self.context.get("session_id"):
            raise ValueError("Subagent belongs to a different conversation")
        runtime = raw.get("metadata", {}).get("model_runtime", {})
        for field, expected in (("provider", provider), ("api_base_url", base)):
            if field in runtime and (runtime[field] or "") != (expected or ""):
                raise ValueError("Restore the worker's original provider and API address before continuing")
        if row.get("connection_binding") is not None and runtime.get("primary_model") != row["model"]:
            raise ValueError("Worker transcript does not match its frozen model")
        return raw

    async def _manage_worker(self, action, agent_id, prompt, timeout_ms, agent_ids, run_in_background):
        tasks = self.context["subagent_tasks"]
        encode = lambda data: json.dumps(data, ensure_ascii=False)
        if action == "capacity":
            active = len(tasks.running)
            return encode({"limit": tasks.limit, "active": active, "available": None if tasks.limit is None else max(0, tasks.limit - active),
                           "queued": len(tasks.pending), "auto_queue": True, "queue_limit": tasks.queue_limit,
                           "max_batch_size": 10, "resume": True, "messages": True, "wait_timeout_ms": 60000, "collaboration": self.context.get("collaboration"),
                           "delegation_usage": self.context.get("collaboration_run")})
        if action == "list":
            return encode({"agents": self._inventory()})
        if action == "resume_queue":
            return await self._resume_queue(agent_ids)
        if action == "wait":
            ids = agent_ids or ([agent_id] if agent_id else list(tasks.tasks))
            for identity in ids:
                self._worker(identity)
            live = [tasks.tasks[i] for i in ids if i in tasks.tasks and not tasks.tasks[i].done()]
            if live:
                await asyncio.wait(live, timeout=timeout_ms / 1000, return_when=asyncio.FIRST_COMPLETED)
            rows = [r for r in self._inventory() if r["agent_id"] in ids]
            tasks.delivered.update(r["agent_id"] for r in rows if r["status"] not in {"queued", "running"})
            return encode({"agents": rows, "timed_out": bool(live) and all(not t.done() for t in live)})
        row = self._worker(agent_id)
        active = tasks.tasks.get(agent_id)
        running = bool(active and not active.done())
        if action == "result":
            return encode(next(r for r in self._inventory() if r["agent_id"] == agent_id))
        if action == "cancel":
            if running:
                await tasks.cancel(agent_id)
            if self.context.get("command_sessions"):
                await self.context["command_sessions"].close(owner=agent_id)
            current = self._worker(agent_id)
            if current.get("status") not in {"cancelled", "completed", "crashed", "closed"}:
                await self._write_subagent_registry_record({"agent_id": agent_id, "status": "cancelled", "finished_at": int(time.time() * 1000)})
            return encode({"agent_id": agent_id, "status": self._worker(agent_id)["status"], "executing": False})
        if action == "close":
            if running:
                raise ValueError("Cannot close a running worker; cancel or wait first")
            if self.context.get("command_sessions"):
                await self.context["command_sessions"].close(owner=agent_id)
            await self._write_subagent_registry_record({"agent_id": agent_id, "status": "closed"})
            return encode({"agent_id": agent_id, "status": "closed", "history_retained": True})
        if action == "send":
            if row.get("status") == "closed":
                raise ValueError("Worker is closed")
            if not prompt.strip():
                raise ValueError("A message prompt is required")
            pending = list(row.get("pending_messages", []))
            if len(pending) >= 100:
                raise ValueError("Worker mailbox is full")
            message = {"id": str(uuid.uuid4()), "text": prompt, "at": int(time.time() * 1000)}
            await self._write_subagent_registry_record({"agent_id": agent_id, "pending_messages": pending + [message]})
            return encode({"agent_id": agent_id, "message_id": message["id"], "queued": True,
                           "executing": agent_id in tasks.running, "requires_followup": not running})
        if action != "followup":
            raise ValueError("Unknown subagent action")
        if running:
            raise ValueError("Worker is running; use send to queue a message")
        if row.get("status") == "closed":
            raise ValueError("Worker is closed")
        if not prompt.strip():
            raise ValueError("A followup prompt is required")
        from xuloom.agents.collaboration import require_delegation
        require_delegation(self.context)
        tasks.check_capacity(1)
        state = self._resume_state(row)
        definition = row.get("role_definition") or state.get("metadata", {}).get("delegation", {}).get("role_definition")
        if not definition:
            raise ValueError("This legacy worker has no frozen role; start a new worker")
        from xuloom.agents.collaboration import admit
        admit(self.context, 1)
        record = {**row, "status": "running", "phase": "starting", "finished_at": None,
                  "turn": row.get("turn", 1) + 1, "background": run_in_background}
        record.update(status="queued", phase="queued", queued_at=int(time.time() * 1000), started_at=None,
            admission_run_id=self.context["collaboration_run"]["id"],
            dispatch={"task": prompt, "allowed_tools": definition.get("allowed_tools"), "inherit_context": False,
                "parent_depth": row.get("agent_depth", 1) - 1, "parent_agent_id": row.get("parent_agent_id"),
                "builtin_agent_def": definition, "model": row.get("model"), "background": run_in_background, "resume": True,
                "connection_binding": copy.deepcopy(row.get("connection_binding"))})
        await self._write_subagent_registry_record(record)
        work = self._run_registered_subagent(record, task=prompt, allowed_tools=definition.get("allowed_tools"),
            inherit_context=False, parent_depth=row.get("agent_depth", 1) - 1,
            parent_agent_id=row.get("parent_agent_id"), builtin_agent_def=definition,
            model=row.get("model"), child_agent_id=agent_id, background=run_in_background,
            description=row.get("description", ""), resume_state=state, connection_binding=copy.deepcopy(row.get("connection_binding")))
        tasks.launch(agent_id, work, on_cancelled_before_start=self.context.get("register_subagent_record"))
        if not run_in_background:
            await tasks.tasks[agent_id]
            tasks.delivered.add(agent_id)
        return encode(next(r for r in self._inventory() if r["agent_id"] == agent_id))

    async def _resume_queue(self, identities=None):
        from xuloom.agents.collaboration import require_delegation, admit
        require_delegation(self.context)
        tasks = self.context["subagent_tasks"]
        if identities and len(identities) != len(set(identities)):
            raise ValueError("Queue identities must be unique")
        rows = [self._worker(identity) for identity in identities] if identities else [r for r in self.context.get("subagent_registry", []) if r.get("status") == "queued"]
        pending = []
        for row in rows:
            live = tasks.tasks.get(row["agent_id"])
            if live and not live.done():
                continue
            if row.get("status") != "queued" or not row.get("dispatch"):
                raise ValueError("Only saved, never-started queued workers can be resumed; running/interrupted work is not replayed")
            self._check_worker_connection(row)
            arguments = copy.deepcopy(row["dispatch"])
            if arguments.get("connection_binding") != row.get("connection_binding"):
                raise ValueError("Queued dispatch does not match its frozen connection")
            if arguments.pop("resume", False):
                arguments["resume_state"] = self._resume_state(row)
            pending.append((row, arguments))
        tasks.check_capacity(len(pending))
        current = self.context.get("collaboration_run", {}).get("id")
        charge = sum(row.get("admission_run_id") != current for row, _ in pending)
        if charge:
            admit(self.context, charge)
        coroutines = {}
        try:
            for row, arguments in pending:
                await self._write_subagent_registry_record({"agent_id": row["agent_id"], "status": "queued", "phase": "queued",
                    "admission_run_id": self.context["collaboration_run"]["id"]})
                coroutines[row["agent_id"]] = self._run_registered_subagent(row, **arguments,
                    child_agent_id=row["agent_id"], description=row.get("description", ""))
            tasks.launch_batch(coroutines, on_cancelled_before_start=self.context.get("register_subagent_record"))
        except BaseException:
            for coroutine in coroutines.values():
                coroutine.close()
            raise
        ids = {r["agent_id"] for r in rows}
        return json.dumps({"agents": [r for r in self._inventory() if r["agent_id"] in ids], "resumed": len(pending)}, ensure_ascii=False)

    def _mailbox(self, identity):
        return copy.deepcopy(self._worker(identity).get("pending_messages", []))

    def _ack_mailbox(self, identity, message_ids):
        row = self._worker(identity)
        pending = [m for m in row.get("pending_messages", []) if m["id"] not in message_ids]
        writer = self.context.get("register_subagent_record")
        if writer:
            writer({"agent_id": identity, "pending_messages": pending})
