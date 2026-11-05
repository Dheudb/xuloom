"""CLI and desktop use the same detached controller; CLI exit is not cancellation."""
import asyncio


def add_parser(sub):
    p = sub.add_parser("scaling", help="Opt-in high-usage SSH swarm workbench")
    p.add_argument("action", choices=("nodes", "save-node", "remove-node", "check", "start", "list", "status", "tasks", "result", "children", "child_result", "child_retry", "events", "message", "messages", "questions", "answer", "pause", "resume", "cancel", "retry", "watch", "shared"))
    p.add_argument("id", nargs="?", help="Run ID, or node ID for check/remove-node")
    p.add_argument("--node", action="append", default=[], help="Configured node ID; repeat for multiple hosts")
    p.add_argument("--ssh", help="Existing SSH profile ID")
    p.add_argument("--local",action="store_true",help="Use this machine as an execution node; no SSH required")
    p.add_argument("--max-agents", type=int, default=8)
    p.add_argument("--manual-capacity", action="store_true")
    p.add_argument("--reserve-percent", type=int, default=25)
    p.add_argument("--api-source", choices=("inherit", "local", "remote"), default="inherit", help="Default inherits the local API; remote host calls provider directly")
    p.add_argument("--pool", action="append", nargs=2, metavar=("CONNECTION", "MODEL"), default=[], help="Select a saved local connection and model; repeat for heterogeneous workers")
    p.add_argument("--group-width", type=int, default=50, help="Workers per group coordinator, not total swarm capacity")
    p.add_argument("--connection", help="Remote connection ID when --api-source remote")
    p.add_argument("--model", help="Model on the remote host")
    p.add_argument("--backend", choices=("native", "codex", "claude", "pi", "dsh"), default="native")
    p.add_argument("--goal")
    p.add_argument("--text", help="Message to the running swarm primary; --request-id makes delivery idempotent")
    p.add_argument("--target", type=int)
    p.add_argument('--output-budget',type=int,help='Run-wide admitted output-token cap, including offline node allocations; native executors only')
    p.add_argument('--admission-seconds',type=int,help='Stop new model admission after this duration; does not undo existing side effects')
    p.add_argument("--request-id", help="Stable start identity; lost replies never create a second swarm")
    p.add_argument("--task", help="Task ID for result/retry")
    p.add_argument("--child", help="Nested task ID for child_result/child_retry; --task selects its node primary")
    p.add_argument("--confirm-side-effects", action="store_true", help="Confirm you reviewed the failed native transcript before retry")
    p.add_argument("--permission", choices=("ask", "full"), default="ask")
    p.add_argument("--accept-high-usage", action="store_true", help="Explicitly enable large-scale model/API consumption")
    p.add_argument("--question", help="Pending question ID")
    p.add_argument("--choice", choices=("allow", "deny"), help="Permission response; never imply approval from a timeout")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--cursor", type=int, default=0)
    p.add_argument("--kind", choices=("claim","note","artifact"), help="Shared record kind")
    p.add_argument("--query", default="", help="Search shared records")
    p.add_argument("--entry", help="Read one full shared record")
    p.add_argument("--json", action="store_true")


async def dispatch(args, emit):
    from .control import invoke
    action = args.action
    if action in {"nodes", "save-node", "remove-node", "check"}:
        cfg = await invoke("config")
        if action == "nodes": value = cfg
        elif action == "save-node":
            if not args.id or not (args.ssh or args.local) or (args.ssh and args.local): raise ValueError("Supply a node ID and either --local or --ssh")
            row = {"id": args.id, "ssh": args.ssh,"location":"local" if args.local else "ssh", "max_agents": args.max_agents, "automatic": not args.manual_capacity,
                "reserve_percent": args.reserve_percent, "connection": args.connection, "model": args.model, "backend": args.backend,
                "api_source": args.api_source, "api_pool": [{"connection": connection, "model": model, "backend": args.backend} for connection, model in args.pool], "group_width": args.group_width}
            value = await invoke("save", {"nodes": [r for r in cfg["nodes"] if r["id"] != args.id] + [row]})
        elif action == "remove-node": value = await invoke("save", {"nodes": [r for r in cfg["nodes"] if r["id"] != args.id]})
        else:
            node = next((r for r in cfg["nodes"] if r["id"] == args.id), None)
            if not node: raise ValueError("Unknown Scaling node")
            value = await invoke("check", {"node": node})
    elif action == "start":
        params = {"goal": args.goal or "", "nodes": args.node, "permission": args.permission, "accept_high_usage": args.accept_high_usage}
        if args.target is not None: params["target"] = args.target
        limits={k:v for k,v in {'output_tokens':getattr(args,'output_budget',None),'seconds':getattr(args,'admission_seconds',None)}.items() if v is not None}
        if limits: params['budget']=limits
        if args.request_id: params["request_id"] = args.request_id
        value = await invoke("create", params)
    elif action == "list": value = await invoke("list")
    elif action == "watch":
        if not args.id: raise ValueError("Supply a run ID")
        while True:
            value = await invoke("status", {"id": args.id})
            await emit({"type": "scaling_status", "run": value})
            if value["status"] in {"completed", "cancelled", "blocked", "paused"}: break
            await asyncio.sleep(2)
    else:
        if not args.id: raise ValueError("Supply a run ID")
        params = {"id": args.id, "offset": args.offset, "cursor": args.cursor}
        if action=="shared":params.update(kind=args.kind,query=args.query,entry=args.entry)
        if action == "message": params.update(text=args.text or "", request_id=args.request_id or __import__("uuid").uuid4().hex)
        if action == "answer":
            if not args.question or not args.choice: raise ValueError("Supply --question and --choice allow|deny")
            params.update(question=args.question, answer=args.choice)
        if action == "retry": params.update(task=args.task, confirm_side_effects=args.confirm_side_effects)
        if action == "result": params.update(task=args.task)
        if action in {"children", "child_result", "child_retry"}: params.update(task=args.task, child=args.child, confirm_side_effects=args.confirm_side_effects)
        value = await invoke(action, params)
    return {"ok": True, "reason": "completed", "scaling": value}
