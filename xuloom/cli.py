"""Non-interactive entry points over QueryEngine; stdout can be a pure JSONL stream."""
import argparse
import asyncio
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import time

from xuloom.core.config import load_config_into_env
from xuloom.core.engine import QueryEngine
from xuloom.core.reasoning import EFFORTS
from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionBusyError, SessionConflictError
from xuloom.features import goals, schedules
from xuloom.security.permissions import ApprovalPolicy

EXIT_CODES = {"completed": 0, "cancelled": 130, "session_busy": 4, "session_conflict": 4, "persistence_error": 5, "remote_disconnected": 9,
    "paused": 6, "blocked": 7, "awaiting_review": 7, "interrupted": 7, "budget_exhausted": 8, "approval_required": 3, "input_required": 3}


def parser():
    p = argparse.ArgumentParser(prog="xuloom", description="Non-interactive tasks, persisted goals and timed messages")
    sub = p.add_subparsers(dest="command", required=True)
    context = sub.add_parser("context", help="Inspect context or explicitly compact an existing session")
    context.add_argument("action", choices=("status", "compact"), nargs="?", default="status")
    context.add_argument("--session", required=True)
    context.add_argument("--cwd", default=os.getcwd())
    context.add_argument("--focus", default="", help="Optional summary focus; does not authorize tool execution")
    context.add_argument("--permission", choices=("ask", "full"), default="ask")
    context.add_argument("--json", action="store_true")
    search = sub.add_parser('search-config', help='Configure the active connection search backend; no model request')
    search.add_argument('--connection', help='Existing connection ID; defaults to the active connection')
    search.add_argument('--backend', choices=('builtin', 'openai', 'openai-chat', 'anthropic', 'deepseek', 'qwen'))
    search.add_argument('--model', help='Optional search model; empty inherits the current model')
    search.add_argument('--base-url', help='Optional search API root; empty reuses the connection address and key')
    search.add_argument('--json', action='store_true')
    credentials = sub.add_parser("credentials", help="Inspect/migrate OS-protected credentials or replace one model key; no model request")
    credentials.add_argument("action", choices=("status", "migrate", "set-model-key"))
    credentials.add_argument("--cwd", help="Also inspect/migrate this project's MCP credentials")
    credentials.add_argument("--connection", help="Existing model connection ID to repair")
    credentials.add_argument("--key-env", help="Name of an environment variable holding the new key; never pass the key as an argument")
    credentials.add_argument("--json", action="store_true")
    mcp = sub.add_parser("mcp", help="Configure HTTP MCP and explicitly sign in/out; no model request")
    mcp.add_argument("action", choices=("list","add","update","remove","login","logout"))
    mcp.add_argument("name", nargs="?")
    mcp.add_argument("--cwd", default=os.getcwd())
    mcp.add_argument("--url")
    mcp.add_argument("--oauth", action="store_true")
    mcp.add_argument("--client-metadata-url")
    mcp.add_argument("--client-id")
    mcp.add_argument("--issuer")
    mcp.add_argument("--redirect-uri")
    mcp.add_argument("--client-secret-env", help="Environment variable holding a registered client secret; never pass the secret on the command line")
    mcp.add_argument("--client-auth", choices=("auto","none","client_secret_basic","client_secret_post"), default="auto")
    mcp.add_argument("--no-browser", action="store_true")
    mcp.add_argument("--json", action="store_true")
    cleanup = sub.add_parser("sandbox-cleanup", help="Inspect or explicitly retry owned Windows isolation cleanup; no model request")
    cleanup.add_argument("--cwd", default=os.getcwd())
    cleanup.add_argument("--id", help="Retry the specified saved cleanup record; omitted lists pending records")
    cleanup.add_argument("--json", action="store_true")
    from xuloom.media.cli import add_parser as add_media_parser
    add_media_parser(sub)
    status = sub.add_parser("git-status", help="Read repository/branch without a model request")
    status.add_argument("--cwd", default=os.getcwd())
    status.add_argument("--json", action="store_true")
    summary = sub.add_parser("summary", help="Read saved output files, sources and tool counts without calling a model")
    summary.add_argument("--cwd", default=os.getcwd())
    summary.add_argument("--session", required=True)
    summary.add_argument("--json", action="store_true")
    selection = summary.add_mutually_exclusive_group()
    selection.add_argument("--turn", help="Human message UUID identifying one turn")
    selection.add_argument("--all", action="store_true", help="All saved turns in this conversation")
    transcript = sub.add_parser("transcript", help="Read a saved conversation or registered child history without executing it")
    transcript.add_argument("--cwd", default=os.getcwd())
    transcript.add_argument("--session", required=True)
    transcript.add_argument("--agent", help="Registered subagent ID in this parent conversation")
    transcript.add_argument("--cursor", help="Continue the exact read-only history snapshot")
    transcript.add_argument("--limit", type=int, default=40)
    transcript.add_argument("--json", action="store_true")
    link = sub.add_parser("chat-link", help="Create or resolve a local/known SSH chat address without calling a model")
    link.add_argument("--cwd", default=os.getcwd())
    mode = link.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session")
    mode.add_argument("--resolve", metavar="URL")
    link.add_argument("--remote", metavar="CONNECTION_ID", help="Verify an existing SSH session and create its link")
    link.add_argument("--json", action="store_true")
    for name in ("exec", "goal", "schedule", "pipeline"):
        item = sub.add_parser(name)
        item.add_argument("--cwd", default=os.getcwd())
        item.add_argument("--session", help="Existing session ID; required for resume/control or scheduling an existing conversation")
        item.add_argument("--json", action="store_true", help="JSON events and one final receipt on stdout; diagnostics on stderr")
        item.add_argument("--permission", choices=("ask", "full"), default="ask", help="ask never waits on stdin; required approvals are rejected explicitly")
        item.add_argument("--skill", action="append", default=[])
        item.add_argument("--sandbox", choices=("host", "workspace"), default=None,
                          help="OS execution isolation, separate from approvals; workspace fails closed if unavailable")
        item.add_argument("--collaboration", choices=("solo", "auto", "ultra"), default=None,
                          help="Delegation policy, independent of reasoning and permissions; resume keeps the saved mode unless supplied")
        item.add_argument("--effort", choices=("default", *EFFORTS), help="Model reasoning effort, independent of collaboration")
        item.add_argument("--thinking", choices=("default", "enabled", "disabled"), help="Requires the model's declared Thinking parameter format")
        if name == "pipeline":
            item.add_argument("action", choices=("start", "resume", "tree"))
            item.add_argument("--run-id", help="Stable run identity; required for start/resume")
            item.add_argument("--spec", help="UTF-8 JSON file containing items and stages (start only)")
        elif name == "exec": item.add_argument("prompt", nargs="?", help="Task text, or - to read UTF-8 stdin")
        else:
            item.add_argument("action", choices=("start", "run", "pause", "resume", "cancel", "accept", "status") if name == "goal" else ("add", "watch", "list", "pause", "resume", "cancel", "serve"))
            item.add_argument("prompt", nargs="?", help="Goal objective or scheduled message")
            if name == "goal":
                item.add_argument("--criterion", action="append", default=[])
                item.add_argument("--checks", default="[]", help="JSON array of explicit file_exists/file_contains checks; otherwise completion requires host/user acceptance")
                item.add_argument("--rounds", type=int, default=20)
                item.add_argument("--seconds", type=float, default=1800)
                item.add_argument("--output-tokens", type=int, default=50000)
                item.add_argument("--reason", default="")
            else:
                item.add_argument("--id")
                when = item.add_mutually_exclusive_group()
                when.add_argument("--delay", type=float, help="Seconds from now")
                when.add_argument("--at", help="ISO 8601 due time with timezone")
                item.add_argument("--interval", type=float, help="Repeat every N seconds, minimum 60; missed occurrences coalesce")
                item.add_argument("--once", action="store_true", help="Process current due messages then exit")
                item.add_argument("--worker", action="append", default=[], help="Wait for all these existing worker IDs (watch only)")
                item.add_argument("--marker-file", help="Workspace completion marker (watch only, max 64 KiB)")
                item.add_argument("--request-id", help="Stable deduplication identity for an authorized follow-up")
    from xuloom.remote.cli import add_parser as add_remote_parser
    add_remote_parser(sub)
    return p


async def stream(engine, prompt, skills, emit):
    final = None
    async for event in engine.submit_message(prompt, selected_skills=skills, sys_print_callback=lambda _: None):
        if event["type"] == "final": final = event["result"]
        else: await emit(event)
    final = final or {"ok": False, "reason": "missing_result"}
    needed = "approval_required" if getattr(engine, "_noninteractive_approval_required", False) else "input_required" if getattr(engine, "_noninteractive_question_required", False) else None
    return {**final, "ok": False, "execution_reason": final["reason"], "reason": needed} if needed else final


async def open_engine(args, emit, *, read_only=False):
    cwd = Path(args.cwd).resolve(strict=True)
    if not cwd.is_dir(): raise ValueError("Working directory must be a directory")
    load_config_into_env()
    async def approval(request):
        engine._noninteractive_approval_required = True
        await emit({"type": "approval_required", "tool_name": request.tool_name,
            "message": "Non-interactive execution cannot approve this action; no stdin prompt was opened"})
        engine.abort_event.set()
        return "deny"
    engine = QueryEngine(cwd=str(cwd), permission_handler=approval, approval_policy=ApprovalPolicy(args.permission))
    async def question(questions):
        engine._noninteractive_question_required = True
        await emit({"type": "input_required", "questions": questions})
        engine.abort_event.set()
        return "No interactive user is present. This task is blocked awaiting explicit user input; do not assume an answer."
    engine.tool_context["ask_user_callback"] = question
    try:
        if args.session:
            clients = [engine.client, engine.compactor.client]
            if not engine.load_session(args.session): raise ValueError("Session does not exist in this directory")
            for client in clients:
                if client and client is not engine.client and client is not engine.compactor.client:
                    await client.close()
            engine.permission_manager.approval_policy.set_mode(args.permission)
        if read_only: return engine
        engine.require_writer()
        if getattr(args, "sandbox", None) is not None:
            engine.execution_sandbox.mode = args.sandbox
            engine.persist_session_state()
        if getattr(args, "collaboration", None) is not None:
            engine.set_collaboration_mode(args.collaboration)
        if getattr(args, "effort", None) is not None or getattr(args, "thinking", None) is not None:
            engine.set_reasoning_options(thinking=args.thinking, effort=args.effort)
            if not engine.persist_session_state():
                raise OSError("推理设置保存失败")
        await engine.start_protocols(lambda _: None)
        await emit({"type": "session_started", "session_id": engine.session_id, "cwd": engine.session_cwd})
        return engine
    except BaseException:
        await engine.aclose()
        raise


async def dispatch(args, emit):
    if args.command == "context":
        from xuloom.context.compaction import status
        engine = await open_engine(args, emit, read_only=args.action == "status")
        try:
            if args.action == "compact":
                engine.abort_event.clear()
                await engine._apply_layered_compaction_if_needed(lambda _: None, force=True, focus=args.focus[:4000])
            return {"ok": True, "reason": "completed", "context": status(engine), "session_id": engine.session_id}
        finally: await engine.aclose()
    if args.command == 'search-config':
        from xuloom.core.config import load_config, update_config
        from xuloom.core.model_catalog import connections, active_connection
        from xuloom.tools.native_search import normalize
        def select(cfg):
            identity = args.connection or active_connection(cfg)['id']
            item = next((row for row in connections(cfg) if row['id'] == identity), None)
            if item is None: raise ValueError('请先添加模型连接')
            return item
        if any(value is not None for value in (args.backend, args.model, args.base_url)):
            def change(cfg):
                item = select(cfg)
                profile = normalize(item.get('native_search'))
                for name, value in [('backend', args.backend), ('model', args.model), ('baseUrl', args.base_url)]:
                    if value is not None: profile[name] = value
                item['native_search'] = normalize(profile)
                cfg['model_connections'] = [item if row['id'] == item['id'] else row for row in connections(cfg)]
                return cfg
            cfg = update_config(change)
        else:
            cfg = load_config(decrypt_secrets=False)
        item = select(cfg)
        await emit({'type': 'search_config', 'connection_id': item['id'], 'search': normalize(item.get('native_search'))})
        return {'ok': True, 'reason': 'completed', 'connection_id': item['id'], 'search': normalize(item.get('native_search'))}
    if args.command == "mcp":
        from xuloom.branding import configuration_file
        from xuloom.protocols.mcp_config import MCPConfigStore, validate_server
        store=MCPConfigStore(configuration_file(Path(args.cwd).resolve(),"mcp_servers.json"))
        if args.action=="list":
            issues=[]
            servers=await asyncio.to_thread(store.snapshot,issues=issues)
            return {"ok":True,"reason":"completed","servers":servers,"configurationIssues":issues}
        if not args.name: raise ValueError("Provide an MCP server name")
        if args.action in {"add","update"}:
            value={"type":"http","url":args.url,"enabled":False}
            if args.action=="update":
                previous=(await asyncio.to_thread(store.read,include_skills=True))["mcpServers"].get(args.name)
                if not isinstance(previous,dict):raise ValueError("MCP server no longer exists")
                value["enabled"]=previous.get("enabled",True)
            fixed_client=any(value is not None for value in (args.client_id,args.issuer,args.redirect_uri,args.client_secret_env)) or args.client_auth!="auto"
            if not args.oauth and (fixed_client or args.client_metadata_url is not None):
                raise ValueError("OAuth client options require --oauth")
            if args.oauth:
                value["auth"]={"type":"oauth"}
                if args.client_metadata_url: value["auth"]["client_metadata_url"]=args.client_metadata_url
                if fixed_client:
                    value["auth"].update(client_id=args.client_id,issuer=args.issuer,redirect_uri=args.redirect_uri,token_endpoint_auth_method=args.client_auth)
                    if args.client_secret_env is not None:
                        if not args.client_secret_env:raise ValueError("Provide the name of the client secret environment variable")
                        secret=os.environ.get(args.client_secret_env)
                        if not secret:raise ValueError("The client secret environment variable is missing or empty")
                        value["auth"]["client_secret"]=secret
            await asyncio.to_thread(store.save,args.name,value,existing=args.action=="update")
            return {"ok":True,"reason":"completed","servers":await asyncio.to_thread(store.snapshot)}
        if args.action=="remove":
            await asyncio.to_thread(store.remove,args.name)
            return {"ok":True,"reason":"completed"}
        from xuloom.protocols.mcp_bridge import runtime_info
        if not runtime_info()["available"]: raise ValueError(runtime_info()["message"])
        from xuloom.protocols.oauth_store import OAuthStore
        from xuloom.protocols.oauth_callback import OAuthCallback
        from xuloom.protocols.mcp_oauth import login, logout
        raw=(await asyncio.to_thread(store.read,include_skills=True))["mcpServers"].get(args.name)
        value=validate_server(raw)
        if value.get("auth",{}).get("type")!="oauth": raise ValueError("Configure OAuth for this MCP service first")
        receipt=OAuthStore(store,args.name,value)
        if args.action=="logout": result=await logout(receipt)
        else:
            async def notify(event):
                await emit({"type":"mcp_oauth_authorize","name":args.name,**event})
                if not args.json: print("Open this authorization URL: "+event["url"],file=sys.stderr)
                if not args.no_browser:
                    import webbrowser
                    await asyncio.to_thread(webbrowser.open,event["url"])
            result=await login(receipt,OAuthCallback(),notify)
        return {"ok":True,"reason":"completed",**result}
    if args.command == "credentials":
        from xuloom.core.config import load_config, update_config
        from xuloom.core.model_catalog import active_connection, connections, project_active, public_config
        from xuloom.security.credentials import storage_status
        if args.action == "set-model-key":
            key = os.environ.get(args.key_env or "", "")
            if not args.connection or not key:
                raise ValueError("Provide --connection and --key-env naming a non-empty environment variable")
            from xuloom.security.credentials import api_key as checked_key
            key = checked_key(key)
            def change(cfg):
                items = connections(cfg)
                item = next((row for row in items if row["id"] == args.connection), None)
                if item is None:
                    raise ValueError("Unknown model connection")
                item["api_key"] = key
                return project_active(dict(cfg), active_connection({**cfg, "model_connections": items}), items)
            cfg = update_config(change)
        elif args.action == "migrate":
            cfg = update_config(lambda value: value)
        else:
            cfg = load_config()
        output = {"ok": True, "reason": "completed", "credentials": storage_status(cfg), "config": public_config(cfg)}
        if args.cwd:
            from xuloom.branding import configuration_file
            from xuloom.protocols.mcp_config import MCPConfigStore
            workspace = Path(args.cwd).resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("Working directory must be a directory")
            store = MCPConfigStore(configuration_file(workspace, "mcp_servers.json"))
            data = store.read()
            if args.action == "migrate" and store.path.is_file():
                store.migrate()
            output["mcp"] = storage_status(data)
        return output
    if args.command == "sandbox-cleanup":
        from xuloom.security.sandbox_cleanup import pending, retry
        report = retry(args.cwd, args.id) if args.id else None
        ok = report is None or report["status"] == "cleaned"
        return {"ok": ok, "reason": "completed" if ok else "blocked", "cleanup_issues": pending(args.cwd), "report": report}
    if args.command == "pipeline" and args.action == "tree":
        from xuloom.agents.pipeline import read_tree
        if not args.session: raise ValueError("--session is required")
        return {"ok": True, "reason": "completed", **read_tree(args.cwd, args.session, args.run_id)}
    if args.command == "transcript":
        from xuloom.features.agent_history import inspect
        return {"ok": True, "reason": "completed", "transcript": await inspect(args.cwd, args.session,
            agent_id=args.agent, cursor=args.cursor, limit=args.limit)}
    if args.command == "media":
        from xuloom.media.cli import dispatch as media_dispatch
        return {"ok": True, "reason": "completed", **await media_dispatch(args, emit)}
    if args.command == "summary":
        from xuloom.features.session_summary import read_summary
        return {"ok": True, "reason": "completed", "summary": read_summary(args.cwd, args.session, turn_id=args.turn, all_turns=args.all)}
    if args.command == "remote":
        from xuloom.remote.cli import dispatch as remote_dispatch
        return await remote_dispatch(args, emit)
    if args.command == "chat-link":
        from xuloom.features.chat_links import chat_link, remote_chat_link, resolve_link
        if getattr(args, "remote", None) and args.resolve: raise ValueError("--remote cannot be combined with --resolve")
        if getattr(args, "remote", None):
            from xuloom.remote.config import resolve
            url = remote_chat_link(resolve(args.remote), args.session)
        else: url = args.resolve if args.resolve else chat_link(args.cwd, args.session)
        row = resolve_link(url)
        if getattr(args, "remote", None):
            from xuloom.remote.proxy import SSHBridge
            bridge = SSHBridge(row["connection_id"], row["binding"], row["machine"], lambda _: None)
            try:
                connection = await bridge.dispatch("remote.connection", {})
                await bridge.dispatch("initialize", {"sessionId": row["session_id"]})
                row.update(remote_verified=True, machine=connection["machine"])
            finally: await bridge.close()
        return {"ok": True, "reason": "completed", "url": url, "chat": row,
                **({"text": url} if not args.resolve else {})}
    if args.command == "git-status":
        from xuloom.features.workspace_files import git_status
        return {"ok": True, "reason": "completed", "git": await git_status(args.cwd)}
    if args.command == "schedule" and args.action in {"list", "pause", "resume", "cancel"}:
        return {"ok": True, "reason": "completed", "data": schedules.list_items() if args.action == "list" else schedules.change(args.id, args.action)}
    if args.command == "goal" and args.action == "status":
        if not args.session: raise ValueError("--session is required")
        with SessionManager(cwd=args.cwd) as manager:
            raw = manager.load_session_state(args.session)
            goal = goals.snapshot(manager, args.session, raw.get("metadata", {}).get("goal"))
        if not raw.get("_valid"): raise ValueError("Session not found")
        return {"ok": True, "reason": "completed", "goal": goal}
    if args.command == "goal" and args.action in {"pause", "cancel"}:
        if not args.session: raise ValueError("--session is required")
        with SessionManager(cwd=args.cwd) as manager:
            raw = manager.load_session_state(args.session)
        goal = raw.get("metadata", {}).get("goal")
        if not goal: raise ValueError("Session has no goal")
        try:
            with SessionManager(cwd=args.cwd) as manager: manager.acquire_writer(args.session)
        except SessionBusyError:
            goals.request_control(args.cwd, args.session, goal["id"], args.action)
            return {"ok": True, "reason": "completed", "control_requested": args.action, "session_id": args.session}
    if args.command == "schedule" and args.action == "serve":
        failures = 0
        while True:
            for row in schedules.list_items(due=True):
                args.cwd, args.session = row["cwd"], row["session_id"]
                engine = None
                try:
                    engine = await open_engine(args, emit)
                    receipt = await schedules.deliver(engine, row["id"], lambda text, skills: stream(engine, text, skills, emit))
                    if receipt["status"] != "waiting": await emit({"type": "scheduled_delivery", "id": row["id"], "receipt": receipt})
                    if receipt["status"] in {"failed", "blocked", "interrupted"}: failures += 1
                except (SessionBusyError, SessionConflictError):
                    await emit({"type": "schedule_queued", "id": row["id"], "reason": "session_busy_or_conflict"})
                except Exception as error:
                    failures += 1
                    await emit({"type": "schedule_error", "id": row["id"], "error": type(error).__name__})
                    # A missing/moved target is not recreated and never spun on by this worker.
                    with schedules.store() as items:
                        items[row["id"]]["status"], items[row["id"]]["reason"] = "blocked", "Target or delivery failed; inspect the existing session"
                finally:
                    if engine: await engine.aclose()
            if args.once: return {"ok": failures == 0, "reason": "completed" if failures == 0 else "error", "failed_deliveries": failures}
            await asyncio.sleep(5)
    if args.command == "goal" and args.action != "start" and not args.session:
        raise ValueError("--session is required")
    if args.command == "pipeline":
        if not args.run_id: raise ValueError("--run-id is required")
        if args.action == "resume" and not args.session: raise ValueError("--session is required for resume")
        if args.action == "start" and not args.spec: raise ValueError("--spec is required for start")
    engine = await open_engine(args, emit)
    try:
        if args.command == "pipeline":
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.action == "start" else {}
            if not isinstance(spec, dict) or set(spec) != {"items", "stages"} and args.action == "start":
                raise ValueError("Pipeline spec must contain only items and stages")
            receipt = await engine.agents("pipeline", run_id=args.run_id, **spec)
            complete = receipt["ok"] and receipt["data"]["runs"][0]["status"] == "completed"
            result = {"ok": bool(complete), "reason": "completed" if complete else "blocked", "receipt": receipt}
        elif args.command == "exec":
            prompt = sys.stdin.read() if args.prompt == "-" or args.prompt is None and not sys.stdin.isatty() else args.prompt
            if not prompt or not prompt.strip(): raise ValueError("Task text is required")
            result = await stream(engine, prompt, args.skill, emit)
        elif args.command == "goal":
            if args.action == "start":
                goals.create(engine, args.prompt, args.criterion, checks=json.loads(args.checks), rounds=args.rounds,
                    seconds=args.seconds, output_tokens=args.output_tokens, skills=args.skill)
            if args.action in {"start", "run", "resume"}:
                goal = await goals.run(engine, emit)
            else: goal = await goals.control(engine, args.action, args.reason)
            result = {"ok": goal["status"] == "completed", "reason": goal["status"], "goal": goal, "text": (goal.get("last_result") or {}).get("text", "")}
        elif args.action == "watch":
            if bool(args.worker) == bool(args.marker_file): raise ValueError("Choose --worker or --marker-file")
            if args.interval is not None or args.delay is not None or args.at is not None:
                raise ValueError("watch follows its condition; do not supply time or interval options")
            trigger = {"kind": "workers", "agent_ids": args.worker} if args.worker else {"kind": "file", "path": args.marker_file}
            result = {"ok": True, "reason": "completed", "schedule": schedules.add(engine, args.prompt,
                due_at=time.time(), trigger=trigger, request_id=args.request_id, skills=args.skill)}
        else:
            if args.delay is None and args.at is None: raise ValueError("Supply --delay or --at")
            if args.delay is not None:
                if args.delay < 0: raise ValueError("Delay cannot be negative")
                due = time.time() + args.delay
            else:
                from datetime import datetime
                when = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                if when.tzinfo is None: raise ValueError("--at requires a timezone")
                due = when.timestamp()
            result = {"ok": True, "reason": "completed", "schedule": schedules.add(engine, args.prompt,
                due_at=due, interval=args.interval, skills=args.skill)}
        return {**result, "session_id": engine.session_id}
    finally: await engine.aclose()


def main(argv):
    # JSONL is a UTF-8 wire protocol, independent of Windows' active code page
    # and PYTHONIOENCODING. The bundled -I interpreter ignores that env option.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "remote" and args.action == "chat":
        if args.json: parser().error("remote chat is interactive; use remote exec --json for JSONL")
        from xuloom.remote.terminal import interactive_chat
        from xuloom.remote.config import resolve
        try:
            result = asyncio.run(interactive_chat(args.id, session_id=args.session, binding=resolve(args.id),
                permission=args.permission, collaboration=args.collaboration, execution=args.sandbox))
            return 0 if result["ok"] else EXIT_CODES.get(result["reason"], 1)
        except KeyboardInterrupt: return 130
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr); return 1
    wire = sys.stdout
    async def emit(event):
        if args.json:
            wire.write(json.dumps(event, ensure_ascii=False, default=str) + "\n"); wire.flush()
    try:
        with redirect_stdout(sys.stderr): result = asyncio.run(dispatch(args, emit))
    except KeyboardInterrupt: result = {"ok": False, "reason": "cancelled"}
    except Exception as error:
        from xuloom.core.run_result import PersistenceError
        from xuloom.features.chat_links import ChatLinkError
        reason = error.code if isinstance(error, ChatLinkError) else "session_busy" if isinstance(error, SessionBusyError) else "session_conflict" if isinstance(error, SessionConflictError) else "persistence_error" if isinstance(error, PersistenceError) else "error"
        result = {"ok": False, "reason": reason, "error": type(error).__name__}
        print(str(error) if isinstance(error, (ValueError, SessionBusyError, SessionConflictError, PersistenceError)) else type(error).__name__, file=sys.stderr)
    if args.json: wire.write(json.dumps({"type": "final", "result": result}, ensure_ascii=False, default=str) + "\n")
    else: wire.write(str(result.get("text") or json.dumps(result, ensure_ascii=False, default=str)) + "\n")
    wire.flush()
    return EXIT_CODES.get(result["reason"], 1 if not result.get("ok") else 0)
