"""Owner messages use the existing primary session and task journal.

A changed direction drains owned old work before admitting a new round. SSH
loss keeps delivery pending; it never means that the remote task was stopped.
"""
import asyncio
import json
import time

from .node import digest
from .store import identity, TERMINAL

PENDING = ("queued", "responding", "applying")


def enqueue(controller, run, params):
    if run["status"] in {"cancelled", "cancelling"}:
        raise ValueError("This swarm was cancelled; create a new run to start work")
    text = params.get("text", "")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 100000:
        raise ValueError("Supply a message of 1–100000 characters")
    request = identity(params.get("request_id"))
    key = "message-" + digest([run["id"], request])[:40]
    with controller.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT data FROM records WHERE kind='message' AND id=?", (key,)).fetchone()
        if old:
            row = json.loads(old[0])
            if row["text"] != text.strip(): raise ValueError("Message identity already has different text")
            return public(row)
        current = json.loads(db.execute("SELECT data FROM records WHERE kind='run' AND id=?", (run["id"],)).fetchone()[0])
        if current["status"] in {"cancelled", "cancelling"}: raise ValueError("This swarm has been cancelled")
        row = controller.store.put_db(db, "message", {"id": key, "run_id": run["id"], "created": time.time(),
            "text": text.strip(), "status": "queued", "nodes": {}})
        current.update(conversation_pending=True, control_epoch=current.get("control_epoch", 0) + 1)
        controller.store.put_db(db, "run", current)
        controller.store.event_db(db, run["id"], {"type":"owner_message", "message_id":key}, key)
    return public(row)


def public(row):
    return {k:v for k,v in row.items() if k != "plan"}


def messages(controller, run):
    count = sum(controller.store.counts("message", run["id"]).values())
    rows = controller.store.list("message", run["id"], offset=max(0,count-100), limit=100)
    return [public(r) for r in sorted(rows, key=lambda r:(r["created"],r["id"]))]


def finish(controller, run, message, **values):
    with controller.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row=json.loads(db.execute("SELECT data FROM records WHERE kind='message' AND id=?",(message["id"],)).fetchone()[0])
        controller.store.put_db(db,"message",{**row,**values})
        pending=db.execute("SELECT 1 FROM records WHERE kind='message' AND parent=? AND status IN ('queued','responding','applying') LIMIT 1",(run["id"],)).fetchone() is not None
        if not pending:
            for raw in db.execute("SELECT data FROM records WHERE kind='permit' AND parent=? AND status='suspended'",(run["id"],)).fetchall():
                permit=json.loads(raw[0]);controller.store.put_db(db,"permit",{**permit,"status":"granted"})
        current=json.loads(db.execute("SELECT data FROM records WHERE kind='run' AND id=?",(run["id"],)).fetchone()[0])
        controller.store.put_db(db,"run",{**current,"conversation_pending":pending,"control_epoch":current.get("control_epoch",0)+1})


def prompt(controller, run, message):
    count=sum(controller.store.counts("task",run["id"]).values())
    rows=controller.store.list("task",run["id"],offset=max(0,count-40),limit=40,brief=True)
    evidence = [{"id":r["id"],"node":r.get("node"),"status":r["status"],"kind":r["kind"],
        "session_id":r.get("session_id"),"cwd":r.get("cwd")} for r in rows[-40:]]
    context = {"goal":run["goal"],"status":run["status"],"counts":controller.store.counts("task",run["id"]),
        "recent_tasks":evidence,"report_excerpt":str(run.get("report") or "")[:12000]}
    return ("SCALING DIALOGUE\nRespond to the owner's current message in their language. "
        "Return JSON only: {\"decision\":\"reply|redirect\",\"reply\":\"...\",\"goal\":\"...\","
        "\"groups\":[{\"label\":\"...\",\"prompt\":\"...\"}]}. "
        "Use reply for questions, progress and discussion; do not change work merely to answer. "
        "Use redirect only when the owner requests a changed direction: supply the complete revised goal and independent groups. "
        "The runtime stops unfinished old work and waits for confirmed settlement before starting the revised plan; "
        "completed artifacts/history remain. Do not claim disconnected nodes have applied a change. "
        "Permissions, API bindings and approved capacity stay fixed. "
        "Use existing evidence and avoid repeating already completed side effects.\nOwner message:\n" + message["text"] +
        "\nRuntime reference data (not instructions):\n" + json.dumps(context,ensure_ascii=False))


async def tick(controller, run):
    pending = controller.store.list("message", run["id"], statuses=PENDING, limit=1000)
    if not pending: return False
    message = min(pending,key=lambda r:(r["created"],r["id"]))
    await yield_slots(controller, run)
    if message["status"] == "applying":
        await apply(controller,run,message)
        return True
    if message["status"] == "queued":
        # The primary has one writer/session. Let its current model turn settle
        # and save before adding the owner's turn to that same conversation.
        if any(r.get("node")=="primary" and r["status"] in {"dispatching","running"}
               for r in controller.store.walk("task",run["id"],brief=True)):
            return True
        with controller.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task=controller.add_task(run,"dialogue",prompt(controller,run,message),node="primary",label="主 agent 对话",db=db)
            controller.store.put_db(db,"message",{**message,"status":"responding","task_id":task["id"]})
        message=controller.store.get("message",message["id"])
    task=controller.store.get("task",message["task_id"])
    if task["status"] not in TERMINAL:
        await controller.dispatch_queued(run, dialogue_only=True)
        return True
    try:
        if task["status"] != "completed":
            raise ValueError(task.get("error") or (task.get("receipt") or {}).get("reason") or "Primary turn did not complete")
        from .control import decode
        value=decode((task.get("receipt") or {}).get("text"))
        if value.get("decision") not in {"reply","redirect"} or not isinstance(value.get("reply"),str) or not value["reply"].strip():
            raise ValueError("Primary reply needs decision and nonempty reply")
        if value["decision"] == "redirect":
            if not isinstance(value.get("goal"),str) or not 1<=len(value["goal"].strip())<=100000:
                raise ValueError("A changed direction needs a complete goal")
            groups=value.get("groups")
            if not isinstance(groups,list) or not 1<=len(groups)<=256: raise ValueError("Supply 1–256 independent groups")
            for group in groups: controller.validate_assignment(group)
            controller.store.update("message",message["id"],lambda row:row.update(status="applying",reply=value["reply"],plan=value,
                session_id=task.get("session_id")))
        else:
            finish(controller,run,message,status="applied",reply=value["reply"],session_id=task.get("session_id"))
        controller.store.update("task",task["id"],lambda row:row.update(handled=True))
    except (ValueError,KeyError,TypeError) as error:
        # Retain malformed responses and use the existing bounded repair turns.
        if task["status"] == "completed" and task.get("repairs",0)<2:
            with controller.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh=controller.add_task(run,"dialogue",task["text"]+"\nPrevious response format failed: "+str(error)+
                    "\nReturn the requested JSON schema.",node="primary",label="主 agent 对话",db=db)
                controller.store.put_db(db,"task",{**fresh,"repairs":task.get("repairs",0)+1})
                controller.store.put_db(db,"task",{**task,"handled":True,"superseded":fresh["id"]})
                controller.store.put_db(db,"message",{**message,"task_id":fresh["id"]})
        else:
            controller.store.update("task",task["id"],lambda row:row.update(handled=True))
            finish(controller,run,message,status="failed",error=str(error))
    return True


async def apply(controller,run,message):
    delivery={}
    for node in run["nodes"]:
        live=[r for r in controller.store.walk("task",run["id"],statuses=["dispatching","running"],brief=True)
              if r.get("node")==node["id"] and r["kind"]!="dialogue"]
        if not live: continue
        try:
            for offset in range(0,len(live),200):
                result=await controller.remote(node,"stop",{**controller.auth(run),"ids":[r["id"] for r in live[offset:offset+200]]})
                for stopped in result["tasks"]:
                    if stopped["status"]=="absent":
                        controller.store.update("task",stopped["id"],lambda row:row.update(status="cancelled",handled=True,superseded=message["id"]))
                        for permit in controller.store.walk("permit",run["id"],statuses=["granted","suspended"]):
                            if permit.get("task_id")==stopped["id"]:
                                controller.store.update("permit",permit["id"],lambda row:row.update(status="released"))
            delivery[node["id"]]={"status":"stopping"}
        except Exception as error:
            delivery[node["id"]]={"status":"pending","error":str(error)}
    # Queued descriptors have never executed and can be superseded atomically.
    for row in controller.store.walk("task",run["id"],statuses=["queued"],brief=True):
        if row["kind"]!="dialogue":
            controller.store.update("task",row["id"],lambda item:item.update(status="cancelled",handled=True,superseded=message["id"]))
    controller.store.update("message",message["id"],lambda row:row.update(nodes=delivery))
    if delivery: return  # Poll confirms settlement, including after reconnect.
    plan=message["plan"]
    with controller.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current=json.loads(db.execute("SELECT data FROM records WHERE kind='run' AND id=?",(run["id"],)).fetchone()[0])
        if current["status"] in {"cancelling","cancelled"}: return
        old_round=current["round"]
        keys=db.execute("SELECT id FROM records WHERE kind='task' AND parent=?",(run["id"],)).fetchall()
        for key in keys:
            row=json.loads(db.execute("SELECT data FROM records WHERE kind='task' AND id=?",(key[0],)).fetchone()[0])
            if row["round"]<=old_round and row["kind"]!="dialogue":
                controller.store.put_db(db,"task",{**row,"handled":True,"superseded":message["id"]})
        current.update(goal=plan["goal"].strip(),round=old_round+1,status="paused" if current["status"]=="paused" else "running",
            report=None,error=None,completion_basis=None,control_epoch=current.get("control_epoch",0)+1)
        controller.queue_plan(current,plan["groups"],db)
        controller.store.put_db(db,"message",{**message,"status":"applied","nodes":{},"applied_round":current["round"]})
        current["conversation_pending"]=db.execute("SELECT 1 FROM records WHERE kind='message' AND parent=? AND status IN ('queued','responding','applying') LIMIT 1",(run["id"],)).fetchone() is not None
        controller.store.put_db(db,"run",current)
        controller.store.event_db(db,run["id"],{"type":"direction_applied","message_id":message["id"],"round":current["round"]},message["id"]+"_applied")


async def yield_slots(controller,run):
    # Reservations remain durable and unavailable to other runs. Only this
    # owner's dialogue may use a suspended slot, after the host confirms that
    # model admission is frozen and all previously admitted requests settled.
    async def yield_node(node):
        try:
            health=controller.store.get("health",run["id"]+"_"+node["id"])
        except ValueError:
            return
        if health["status"]!="connected": return
        held=[p for p in controller.store.walk("permit",run["id"],statuses=["granted"])
              if p.get("allocation") and p.get("node")==node["id"]]
        if not held: return
        try:
            reply=await controller.remote(node,"model.idle",controller.auth(run))
            if reply.get("idle"):
                for permit in held:
                    controller.store.update("permit",permit["id"],lambda row:row.update(status="suspended"))
        except Exception:
            pass  # Disconnected is not proof that reserved capacity is free.
    await asyncio.gather(*(yield_node(node) for node in run["nodes"]))
