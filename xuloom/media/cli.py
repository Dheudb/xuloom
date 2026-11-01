"""Read/edit the same media connections as the App, without a model request."""
import json
import os
from pathlib import Path
import re
import sys

from xuloom.media import config, jobs


def add_parser(sub):
    parent = sub.add_parser("media", help="Configure generation APIs and inspect durable media receipts")
    actions = parent.add_subparsers(dest="action", required=True)
    for action in ("connections", "save", "remove", "jobs", "status", "cancel"):
        item = actions.add_parser(action)
        item.add_argument("--json", action="store_true")
        if action == "save":
            item.add_argument("--file", required=True, help="Connection JSON file; - reads UTF-8 JSON from stdin")
            item.add_argument("--api-key-env", help="Read the API key from this environment variable, never a command argument")
        if action == "remove":
            item.add_argument("id"); item.add_argument("--yes", action="store_true", help="Confirm removal; existing provider jobs are not cancelled")
        if action in {"jobs", "status", "cancel"}:
            item.add_argument("--cwd", default=os.getcwd()); item.add_argument("--session", required=True)
        if action in {"status", "cancel"}:
            item.add_argument("job_id"); item.add_argument("--permission", choices=("ask", "full"), default="ask")
            item.add_argument("--wait-ms", type=int, default=0)


async def dispatch(args, emit):
    if args.action == "connections":
        return {"connections": config.public_connections()}
    if args.action == "save":
        value = json.loads(sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig"))
        if args.api_key_env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", args.api_key_env) or not os.environ.get(args.api_key_env):
                raise ValueError("The specified media API key environment variable is invalid or empty")
            value["apiKey"] = os.environ[args.api_key_env]
        return {"connections": config.save(value)}
    if args.action == "remove":
        if not args.yes:
            raise ValueError("Use --yes to confirm removing the connection; provider tasks will not be cancelled")
        return {"connections": config.remove(args.id)}
    if args.action == "jobs":
        return {"jobs": jobs.listing({"session_cwd": os.path.abspath(args.cwd), "session_id": args.session})}
    if args.action in {"status", "cancel"}:
        from xuloom.cli import open_engine
        import uuid
        engine = await open_engine(args, emit)
        try:
            engine.activate_tools(["media_tool"])
            receipt = await engine._execute_tool_wrapper({"id": "media-control-" + uuid.uuid4().hex, "name": "media_tool",
                "input": {"action": args.action, "job_id": args.job_id, "wait_ms": args.wait_ms}}, lambda _: None)
            record = jobs.public(jobs.load(engine.tool_context, args.job_id))
            needed = bool(getattr(engine, "_noninteractive_approval_required", False))
            return {"ok": not receipt["is_error"], "reason": "approval_required" if needed else "failed" if receipt["is_error"] else "completed",
                "job": record, "generation_completed": record["phase"] == "downloaded"}
        finally:
            await engine.aclose()
    raise ValueError("Unknown media command")
