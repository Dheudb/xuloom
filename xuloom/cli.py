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
    status = sub.add_parser("git-status", help="Read repository/branch without a model request")
    status.add_argument("--cwd", default=os.getcwd())
    status.add_argument("--json", action="store_true")
    link = sub.add_parser("chat-link", help="Create or resolve a local chat address without calling a model")
    link.add_argument("--cwd", default=os.getcwd())
    mode = link.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session")
    mode.add_argument("--resolve", metavar="URL")
    link.add_argument("--json", action="store_true")
    for name in ("exec", "goal", "schedule"):
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
        if name == "exec": item.add_argument("prompt", nargs="?", help="Task text, or - to read UTF-8 stdin")
        else:
            item.add_argument("action", choices=("start", "run", "pause", "resume", "cancel", "accept", "status") if name == "goal" else ("add", "list", "pause", "resume", "cancel", "serve"))
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


async def open_engine(args, emit):
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
    if args.command == "remote":
        from xuloom.remote.cli import dispatch as remote_dispatch
        return await remote_dispatch(args, emit)
    if args.command == "chat-link":
        from xuloom.features.chat_links import chat_link, resolve_link
        url = args.resolve if args.resolve else chat_link(args.cwd, args.session)
        row = resolve_link(url)
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
                    await emit({"type": "scheduled_delivery", "id": row["id"], "receipt": receipt})
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
    engine = await open_engine(args, emit)
    try:
        if args.command == "exec":
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
