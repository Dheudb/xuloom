"""Non-interactive SSH commands, sharing the desktop JSONL runtime."""
import asyncio
import sys

from xuloom.remote import config
from xuloom.remote.ssh import SSHClient, RemoteDisconnected, RemoteRequestError


def add_parser(sub):
    parent = sub.add_parser("remote", help="Manage keyless SSH profiles and execute on another Xuloom installation")
    actions = parent.add_subparsers(dest="action", required=True)
    for action in ("list", "add", "remove", "check", "sessions", "inspect", "exec", "chat", "files", "read", "git-status"):
        item = actions.add_parser(action)
        item.add_argument("--json", action="store_true", help="UTF-8 JSON events and final receipt")
        if action != "list": item.add_argument("id", help="Saved SSH connection ID")
        if action == "add":
            item.add_argument("--name"); item.add_argument("--host", required=True)
            item.add_argument("--cwd", required=True, help="Absolute directory on the remote machine")
            item.add_argument("--python", default="python3", help="Remote Python with Xuloom installed")
            item.add_argument("--platform", choices=("posix", "windows"), default="posix")
            item.add_argument("--port", type=int, help="Override the SSH alias's configured port"); item.add_argument("--ssh-config", default="")
        if action in {"exec", "inspect", "chat"}: item.add_argument("--session")
        if action in {"exec", "chat"}:
            item.add_argument("--permission", choices=("ask", "full"), default="ask")
            item.add_argument("--collaboration", choices=("solo", "auto", "ultra"))
        if action == "chat": item.add_argument("--sandbox", choices=("host", "workspace"))
        if action == "exec":
            item.add_argument("prompt", nargs="?", help="Task text, or - for UTF-8 stdin")
            item.add_argument("--resume", action="store_true", help="Continue the saved stopped turn without adding a synthetic prompt")
            item.add_argument("--skill", action="append", default=[])
        if action in {"files", "read"}: item.add_argument("path", nargs="?", default="")


async def dispatch(args, emit):
    if args.action == "chat": raise ValueError("Use the interactive remote chat entry point; remote exec provides JSONL execution")
    if args.action == "list": return {"ok": True, "reason": "completed", "connections": config.profiles()}
    if args.action == "add":
        value = config.save({key: getattr(args, key) for key in ("id", "name", "host", "port", "cwd", "python", "platform", "ssh_config")})
        return {"ok": True, "reason": "completed", "connection": value}
    if args.action == "remove":
        config.remove(args.id); return {"ok": True, "reason": "completed", "removed": args.id}
    if args.action == "inspect" and not args.session: raise ValueError("--session is required")
    if args.action == "exec":
        if args.resume and not args.session: raise ValueError("--resume requires --session")
        if args.resume and args.prompt: raise ValueError("--resume cannot replace the saved prompt")
        text = sys.stdin.read() if args.prompt == "-" else args.prompt
        if not args.resume and (not text or not text.strip()): raise ValueError("Task text is required")
    queue = asyncio.Queue()
    client = SSHClient(config.resolve(args.id), queue.put_nowait)
    session_id = getattr(args, "session", None)
    result = None
    try:
        info = await client.start()
        await emit({"type": "remote_connected", "connection_id": args.id, "machine": info})
        if args.action == "check": result = {"ok": True, "reason": "completed", "machine": info}
        elif args.action == "sessions": result = {"ok": True, "reason": "completed", "sessions": await client.request("sessions.list")}
        elif args.action == "git-status": result = {"ok": True, "reason": "completed", "git": await client.request("git.status")}
        elif args.action in {"files", "read"}:
            value = await client.request("files.list" if args.action == "files" else "files.read", {"path": args.path})
            result = {"ok": True, "reason": "completed", "file": value}
        elif args.action == "inspect":
            initial = await client.request("initialize", {"sessionId": session_id})
            status = await client.request("session.status", {"sessionId": session_id})
            result = {"ok": True, "reason": "completed", "history": initial["history"], "status": status}
        else:
            if not session_id: session_id = (await client.request("sessions.create"))["sessionId"]
            await client.request("initialize", {"sessionId": session_id, "permissionMode": args.permission})
            # Persist the target identity in the caller's event stream before
            # asking for any effect. Never automatically retry prompt RPCs.
            await emit({"type": "session_started", "session_id": session_id, "remote": {"id": args.id, "host": client.profile["host"]}})
            if args.collaboration: await client.request("collaboration.set", {"mode": args.collaboration})
            await client.request("prompt.resume" if args.resume else "prompt", {} if args.resume else {"text": text, "skills": args.skill})
            receipt = None
            needed = None
            while True:
                event = await queue.get()
                if event["type"] == "remote_disconnected": raise RemoteDisconnected(event["message"])
                if event["type"] in {"permission", "questions", "plan_approval"}:
                    needed = "input_required" if event["type"] == "questions" else "approval_required"
                    await emit({"type": needed, "remote": event["remote"], "session_id": session_id})
                    await client.request("stop")
                    continue
                if event["type"] == "final": receipt = event.get("result")
                if event["type"] == "turn_error": receipt = event.get("result") or {"ok": False, "reason": "error", "text": event.get("message", "")}
                if event["type"] == "turn_cancelled" and receipt is None: receipt = {"ok": False, "reason": "cancelled"}
                if event["type"] == "turn_finished": break
                # Bridge final is an execution event here; the CLI emits exactly
                # one final receipt after remote shutdown/save is confirmed.
                await emit({**event, "type": "execution_final" if event["type"] == "final" else event["type"]})
            result = receipt or {"ok": False, "reason": "missing_result"}
            if needed: result = {**result, "ok": False, "reason": needed}
    except asyncio.CancelledError:
        confirmed = False
        try: await asyncio.shield(client.request("stop", timeout=15)); confirmed = True
        except (ConnectionError, ValueError, OSError): pass
        result = {"ok": False, "reason": "cancelled" if confirmed else "remote_disconnected", "stop_confirmed": confirmed,
            "outcome_unknown": not confirmed}
    except RemoteDisconnected as error:
        result = {"ok": False, "reason": "remote_disconnected", "outcome_unknown": True, "message": str(error)}
    except RemoteRequestError as error:
        result = {"ok": False, "reason": error.code, "message": str(error)}
    finally:
        try: await client.aclose()
        except (ConnectionError, ValueError, OSError) as error:
            result = {**(result or {}), "ok": False, "reason": "remote_disconnected", "outcome_unknown": True, "message": str(error)}
    return {**result, "remote": {"id": args.id, "host": client.profile["host"]}, **({"session_id": session_id} if session_id else {})}
