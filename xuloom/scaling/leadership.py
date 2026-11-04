"""An autonomous node primary reuses the same small controller on its own host.

No parent RPC is needed for decomposition, inference, workers or aggregation.
Only explicit human approvals and final cross-host reconciliation cross SSH.
"""
import asyncio
import copy
import json
import math
import secrets

from .control import Controller, task_prompt
from . import models
from .node import digest
from .store import Store


class Leadership(Controller):
    def __init__(self, host):
        self.host = host
        super().__init__(Store(host.store.path.with_name(host.store.path.stem + "-leaders.sqlite3")), local=host)

    async def start(self):
        for execution in self.host.store.walk("execution", statuses=["running"], brief=True):
            if execution.get("kind") != "leader": continue
            try:
                run = self.store.get("run", execution.get("child_run_id"))
                if run.get("parent_task") != execution["id"]: raise ValueError("Coordinator ownership changed")
            except ValueError:
                self.host.store.update("execution", execution["id"], lambda row: row.update(status="interrupted", error="Node coordinator records are missing or inconsistent; inspect saved history before retry"))
        for run in self.store.walk("run", statuses=["starting"]):
            # Reconcile only a durable accepted leader identity; child native
            # executions have already been marked interrupted by the host.
            try:
                owned = self.host.store.get("execution", run["parent_task"]).get("child_run_id") == run["id"]
            except ValueError: owned = False
            self.store.update("run", run["id"], lambda row: row.update(status="running" if owned else "blocked", ownership_missing=not owned, error=None if owned else "Coordinator ownership record is missing; inspect saved history"))
        self.loop_task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            for run in self.store.walk("run", statuses=["running", "paused", "cancelling", "blocked"]):
                if not run.get("ownership_missing") and run["id"] not in self.busy:
                    self.busy.add(run["id"])
                    self.jobs[run["id"]] = asyncio.create_task(self.tick_guard(run))
            await asyncio.sleep(.4)

    async def remote(self, node, method, params):
        original = params.get("run_id")
        values = copy.deepcopy(params)
        if original: values["run_id"] = "lane-" + digest([original, node["id"]])[:40]
        if method == "open":
            parent = self.host.store.get("job", node["parent_job"])
            if parent.get("model_file"):
                pool = models.read(parent["model_file"])
            else:
                from xuloom.agents.connections import resolve_connection
                binding, options = resolve_connection(parent["binding"]["connection_id"], parent["binding"]["model"], frozen=parent["binding"])
                pool = [{"id": "remote-active", "binding": binding, "api_key": options["api_key"], "network_mode": binding["network_mode"], "limits": parent["limits"], "backend": parent["settings"].get("backend", "native")}]
            entry = copy.deepcopy(next(p for p in pool if p["id"] == node["pool_id"]))
            if node.get("direct_group"):values["settings"]["flow_limits"]=copy.deepcopy(entry["limits"])
            entry["limits"] = node["check"]["limits"]
            entry["backend"] = node["backend"]
            values["provided_models"] = [entry]
            values["settings"]["host_capacity"] = node["host_capacity"]
            values["settings"]["reference_cwd"] = parent["settings"].get("reference_cwd")
            values["settings"].update(shared_run=parent["settings"].get("shared_run",parent["id"]),
                shared_node=parent["settings"].get("shared_node","primary"),shared_group=self.store.get("run",original)["parent_task"])
            if node.get("direct_group") and node["id"]=="primary": values["settings"]["coordinator_root"]=True
        result = await self.host.dispatch(method, values)
        if method == "poll":
            for row in result["requests"]: row["run_id"] = original
        return result

    async def tick_guard(self, run):
        await super().tick_guard(run)
        current = self.store.get("run", run["id"])
        counts = self.store.counts("task", run["id"])
        final = current["status"] in {"completed", "cancelled"} or current["status"] == "blocked" and not any(counts.get(k) for k in ("queued", "dispatching", "running"))
        changes = {"leader_status": current["status"], "child_run_id": run["id"], "child_counts": counts, "error": current.get("error")}
        if final:
            ok = current["status"] == "completed"
            reviews = [r for r in self.store.walk("task", run["id"], brief=True) if r["kind"] == "synthesize"]
            changes.update(status="completed" if ok else "cancelled" if current["status"] == "cancelled" else "failed",
                receipt={"ok": ok, "persisted": True, "reason": "completed" if ok else current["status"],
                    "text": current.get("report") or current.get("error") or "Node execution stopped", "completion_basis": "model_report"},
                session_id=reviews[-1].get("session_id") if reviews else None)
        self.host.store.update("execution", run["parent_task"], lambda row: row.update(changes))

    def plan_groups(self, run, groups):
        # Additional cohorts stream through these coordinators; do not allocate
        # millions of Python group objects just because capacity was set high.
        required = min(256, math.ceil(run["target"] / run["group_width"]))
        if len(groups) >= required: return groups
        return [{"label": groups[i % len(groups)]["label"] + " · " + str(i + 1),
            "prompt": groups[i % len(groups)]["prompt"] + "\nOwn complementary exploration partition " + str(i + 1) +
                " of " + str(required) + ". Specialize distinct evidence, hypotheses or validation work for this partition."} for i in range(required)]

    def queue_plan(self,run,groups,db):
        if run.get("direct_group"):
            for group in groups: self.validate_assignment(group)
            groups=[{"label":"探索分组","prompt":json.dumps(groups,ensure_ascii=False)+
                "\nOwn complementary host partition "+str(run["partition"])+" of "+str(run["partitions"])+"; specialize distinct work."}]
        return super().queue_plan(run,groups,db)

    def coordinator_node(self, run, index):
        if run.get("direct_group"): return "primary"
        return run["nodes"][1:][index % (len(run["nodes"]) - 1)]["id"]

    def questions(self, parent_job):
        result = []
        for run in self.store.walk("run"):
            if run.get("parent_job") != parent_job: continue
            result.extend({**row, "run_id": parent_job, "leader_task": run["parent_task"]}
                for row in self.store.list("question", run["id"], statuses=["pending"], limit=1000))
        return result

    def owns_question(self, key, parent_job):
        try:
            question = self.store.get("question", key)
            return self.store.get("run", question["run_id"])["parent_job"] == parent_job
        except ValueError: return False

    async def answer(self, parent_job, params):
        question = self.store.get("question", params["id"])
        run = self.store.get("run", question["run_id"])
        if run["parent_job"] != parent_job: raise ValueError("Question is owned by another node assignment")
        if question["status"] in {"answered", "delivered"}:
            if question.get("answer") != params.get("answer"): raise ValueError("This approval already has a different answer")
            return True
        return await self.dispatch("answer", {"id": run["id"], "question": question["id"], "answer": params.get("answer")})

    def api_jobs(self, parent_job):
        return {"lane-"+digest([r["id"],node["id"]])[:40] for r in self.store.walk("run",brief=True)
            if r.get("parent_job")==parent_job for node in r["nodes"]}

    async def control(self, parent_job, action, *, yield_models=False):
        for run in self.store.walk("run"):
            if run.get("parent_job") == parent_job and run["status"] not in {"completed", "cancelled"}:
                self.store.update("run",run["id"],lambda row:row.update(yield_models=yield_models))
                desired = {"pause": "paused", "resume": "running", "cancel": "cancelling"}[action]
                if run["status"] != desired:
                    await self.dispatch(action, {"id": run["id"]})

    async def inspect(self, parent_job, method, params):
        if method not in {"tasks", "result", "evidence", "retry", "cancel"}: raise ValueError("Unknown node inspection operation")
        execution = self.host.store.get("execution", params["leader_task"])
        if execution["run_id"] != parent_job or execution["kind"] != "leader": raise ValueError("Foreign node leader")
        if method == "retry" and execution["status"] != "running": raise ValueError("Node allocation has ended")
        run = self.store.get("run", execution["child_run_id"])
        if method == "cancel" and run["status"] in {"completed","cancelled"}: return {"requested":True}
        if method == "evidence":
            from .evidence import page
            return page(self.store, run["id"], params["values"])
        return await self.dispatch(method, {"id": run["id"], "task": params.get("task"), "offset": params.get("offset", 0), "confirm_side_effects": params.get("confirm_side_effects")})

    async def close(self):
        if self.loop_task: self.loop_task.cancel()
        for job in list(self.jobs.values()): job.cancel()
        await asyncio.gather(*(list(self.jobs.values()) + ([self.loop_task] if self.loop_task else [])), return_exceptions=True)


async def accept(host, parent, task):
    description = json.loads(task["text"])
    from .config import positive
    target = positive(description.get("target"), "Node worker target")
    width = positive(description.get("group_width", 50), "Group width")
    if target > parent["settings"]["max_agents"] or width > 1000: raise ValueError("Assignment exceeds the node's declared capacity")
    if description.get("direct_group"):
        if target>width: raise ValueError("Group worker capacity exceeds its coordinator width")
        part=positive(description.get("partition",1),"Partition")
        parts=positive(description.get("partitions",1),"Partition count")
        if part>parts: raise ValueError("Group partition exceeds its count")
        directions=description.get("directions")
        if not isinstance(directions,list) or not 1<=len(directions)<=256: raise ValueError("Group directions are required")
        for group in directions: Controller.validate_assignment(group)
    if not isinstance(description.get("goal"), str) or not description["goal"].strip(): raise ValueError("Node goal is required")
    allocations = description.get("allocations")
    pool = [copy.deepcopy(p) for p in parent["pool"] if allocations and p["id"] in allocations]
    if not pool: raise ValueError("No offline model slots have been allocated to this host")
    for entry in pool:
        from xuloom.core.request_flow import limits
        policy = limits(allocations[entry["id"]])
        if policy["concurrency"] > entry["limits"]["concurrency"]: raise ValueError("Node API allocation exceeds its declared limit")
        entry["limits"] = policy
    if not host.leadership:
        host.leadership = Leadership(host)
        await host.leadership.start()
    controller = host.leadership
    run_id = "node-" + task["id"]
    primary = {"id": "primary", "max_agents": 1, "automatic": parent["settings"]["automatic"],
        "reserve_percent": parent["settings"]["reserve_percent"], "backend": "native", "pool_id": pool[0]["id"],
        "host_capacity": parent["settings"]["max_agents"], "parent_job": parent["id"]}
    if description.get("direct_group"): primary["direct_group"]=True
    primary["check"] = {"binding": pool[0]["binding"], "quota": pool[0]["quota"], "limits": pool[0]["limits"],
        "machine": host.check({"provided_models": models.read(parent["model_file"])})["machine"] if parent.get("model_file") else host.check(parent["settings"])["machine"], "program": None}
    nodes = [primary]
    for index, entry in enumerate(pool):
        count = max(1, math.ceil(target / len(pool)))
        node = {**primary, "id": "pool-" + str(index), "max_agents": count, "pool_id": entry["id"], "backend": entry["backend"]}
        program = None
        if entry["backend"] != "native":
            from xuloom.agents.external.program import resolve
            program = resolve(None, backend=entry["backend"], workspace=parent["settings"].get("reference_cwd") or str(host.store.path.parent))
        node["check"] = {"binding": entry["binding"], "quota": entry["quota"], "limits": entry["limits"], "machine": primary["check"]["machine"], "program": program}
        nodes.append(node)
    row = {**copy.deepcopy(task), "run_id": parent["id"], "status": "running", "input_digest": digest(task),
        "child_run_id": run_id, "leader_status": "starting", "cwd": str(host.store.path.parent)}
    host.store.put("execution", row)
    run = {"id": run_id, "status": "starting", "goal": description["goal"], "nodes": nodes, "target": target,
        "permission": parent["settings"]["permission"], "token": secrets.token_hex(32), "round": 1, "group_width": width,
        "parent_job": parent["id"], "parent_task": task["id"], "completion_basis": None}
    run["shared_version"]=task.get("input_version","1")
    if description.get("direct_group"):
        run.update(direct_group=True,partition=part,partitions=parts)
    controller.store.put("run", run)
    try:
        for node in nodes: await controller.open_node(run, node)
        current=controller.store.get("run",run_id)
        if current["status"] in {"starting","paused"}:
            if run.get("direct_group"):
                with controller.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    controller.queue_plan(run,description["directions"],db)
            else:
                controller.add_task(run, "plan", task_prompt("plan", run["goal"], {"directions": description.get("directions"),
                    "target": target, "required_groups": math.ceil(target / width), "group_width": width}), node="primary")
        controller.store.update("run", run_id, lambda value: value.update(status="running") if value["status"]=="starting" else None)
    except Exception as error:
        controller.store.update("run", run_id, lambda value, message=str(error): value.update(status="blocked", error=message))
        host.store.update("execution", task["id"], lambda value, message=str(error): value.update(leader_status="blocked", error=message))
    return host.public(host.store.get("execution", task["id"]))
