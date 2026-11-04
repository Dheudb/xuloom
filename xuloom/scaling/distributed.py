"""Cross-host layer: durable node assignments and preallocated offline API slots."""
import json
import math
from .node import digest
from .store import TERMINAL


def distribute(controller, run, groups, db):
    for group in groups: controller.validate_assignment(group)
    remaining = run["target"]
    nodes = run["nodes"][1:]
    for index, node in enumerate(nodes):
        if remaining <= 0: break
        available = sum(n["max_agents"] for n in nodes[index:])
        count = min(node["max_agents"], math.ceil(remaining * node["max_agents"] / available))
        directions = groups[index::len(nodes)] or [groups[index % len(groups)]]
        width=node.get("group_width",50)
        groups_count=math.ceil(count/width) if run.get("grouped_leaders") else 1
        for part in range(groups_count):
            amount=count//groups_count+(1 if part>=groups_count-count%groups_count else 0) if run.get("grouped_leaders") else count
            chosen=directions[part::groups_count] or [directions[part%len(directions)]]
            text = json.dumps({"goal": run["goal"], "directions":chosen, "target":amount,
                "group_width":width,"direct_group":bool(run.get("grouped_leaders")),"partition":part+1,"partitions":groups_count},ensure_ascii=False)
            controller.add_task(run,"leader",text,node=node["id"],label=node["id"]+(" · 组长 "+str(part+1) if run.get("grouped_leaders") else " · 节点主 agent"),count=amount,db=db)
        remaining -= count


def allocate(controller, run, node, task):
    """A disconnected host retains its grant until confirmed settlement.

    This admission covers Scaling executions, not other clients using the same
    supplier account. Effective supplier limits remain the user's responsibility.
    """
    envelope = json.loads(task["text"])
    if envelope.get("allocations"): return envelope["allocations"]
    result = {}
    with controller.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='permit' AND status IN ('granted','suspended')")]
        for entry in node["check"].get("pool", []):
            quota, policy = entry["quota"], entry["limits"]
            existing = next((row for row in active if row.get("task_id") == task["id"] and row["quota"] == quota and row.get("allocation")), None)
            if existing:
                result[entry["id"]] = {"concurrency": existing["units"], "requests_per_minute": policy["requests_per_minute"] * existing["units"] / policy["concurrency"] if policy["requests_per_minute"] else None}
                continue
            shared = next((result[p["id"]] for p in node["check"]["pool"] if p["quota"] == quota and p["id"] in result), None)
            if shared:
                result[entry["id"]] = shared
                continue
            held = [row for row in active if row["quota"] == quota]
            capacity = min([policy["concurrency"]] + [row["limits"]["concurrency"] for row in held])
            reserve = 1 if capacity > 1 and quota == run["nodes"][0]["check"]["quota"] else 0
            free = capacity - reserve - sum(row.get("units", 1) for row in held)
            peers = sum(n["max_agents"] for n in run["nodes"][1:] if any(p["quota"] == quota for p in n["check"].get("pool", [])))
            share=task["count"] if envelope.get("direct_group") else node["max_agents"]
            desired = max(1, math.ceil((capacity-reserve) * share / max(1, peers)))
            units = min(free, desired, node["max_agents"])
            if units <= 0: continue
            key = "allocation-" + digest([task["id"], quota])[:32]
            row = controller.store.put_db(db, "permit", {"id": key, "run_id": run["id"], "task_id": task["id"],
                "node": node["id"], "quota": quota, "limits": policy, "units": units, "status": "granted", "allocation": True})
            active.append(row)
            result[entry["id"]] = {"concurrency": units,
                "requests_per_minute": policy["requests_per_minute"] * units / capacity if policy["requests_per_minute"] else None}
    return result


async def advance(controller, run):
    tasks = [row for row in controller.store.walk("task", run["id"], brief=True) if row["round"] == run["round"] and not row.get("superseded")]
    if any(row["status"] not in TERMINAL or not row.get("handled") for row in tasks): return
    leaders = [row for row in tasks if row["kind"] == "leader"]
    if not leaders: raise ValueError("The primary could not create node assignments; inspect its saved plan")
    reviews = [row for row in tasks if row["kind"] == "synthesize"]
    if reviews:
        if all(row["status"] != "completed" for row in reviews): raise ValueError("Primary review failed")
        return
    excerpt = min(10000, 500000 // max(1, len(leaders)))
    reports = [{"id": row["id"], "node": row["node"], "status": row["status"], "child_run_id": row.get("child_run_id"),
        "excerpt": str((controller.store.get("task", row["id"]).get("receipt") or {}).get("text", ""))[:excerpt],
        "excerpt_limit": excerpt, "error": row.get("error")} for row in leaders]
    from .control import task_prompt
    controller.add_task(run, "synthesize", task_prompt("review", run["goal"], reports), node="primary")
