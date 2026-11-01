"""Non-interactive SSH commands, sharing the desktop JSONL runtime."""
import asyncio
import sys

from xuloom.remote import config
from xuloom.remote.ssh import SSHClient, RemoteDisconnected, RemoteRequestError


def add_parser(sub):
    parent = sub.add_parser("remote", help="Manage keyless SSH profiles and execute on another Xuloom installation")
    actions = parent.add_subparsers(dest="action", required=True)
    for action in ("list", "add", "remove", "check", "sessions", "inspect", "summary", "exec", "chat", "files", "read", "preview", "attach", "attachments", "download", "git-status", "references", "forget"):
        item = actions.add_parser(action)
        item.add_argument("--json", action="store_true", help="UTF-8 JSON events and final receipt")
        if action not in {"list", "references"}: item.add_argument("id", help="Local reference token" if action == "forget" else "Saved SSH connection ID")
        if action == "forget":
            item.add_argument("--version", required=True); item.add_argument("--confirm", action="store_true")
        if action == "add":
            item.add_argument("--name"); item.add_argument("--host", required=True)
            item.add_argument("--cwd", required=True, help="Absolute directory on the remote machine")
            item.add_argument("--python", default="python3", help="Remote Python with Xuloom installed")
            item.add_argument("--platform", choices=("posix", "windows"), default="posix")
            item.add_argument("--port", type=int, help="Override the SSH alias's configured port"); item.add_argument("--ssh-config", default="")
        if action in {"exec", "inspect", "summary", "chat", "preview", "attach", "attachments", "download"}: item.add_argument("--session")
        if action == "attach": item.add_argument("files", nargs="+", help="Local files to copy into the remote session")
        if action == "attachments": item.add_argument("--cursor", default="", help="Continue a saved attachment listing")
        if action == "download":
            item.add_argument("attachment", help="Saved attachment UUID")
            item.add_argument("destination", help="Local destination filename")
        if action == "summary":
            choice = item.add_mutually_exclusive_group()
            choice.add_argument("--turn")
            choice.add_argument("--all", action="store_true")
        if action in {"exec", "chat"}:
            item.add_argument("--permission", choices=("ask", "full"), default="ask")
            item.add_argument("--collaboration", choices=("solo", "auto", "ultra"))
        if action == "chat": item.add_argument("--sandbox", choices=("host", "workspace"))
        if action == "exec":
            item.add_argument("prompt", nargs="?", help="Task text, or - for UTF-8 stdin")
            item.add_argument("--resume", action="store_true", help="Continue the saved stopped turn without adding a synthetic prompt")
            item.add_argument("--skill", action="append", default=[])
            item.add_argument("--attach", action="append", default=[], help="Local file to transfer before submitting the task (repeatable)")
            item.add_argument("--attachment", action="append", default=[], help="Previously uploaded attachment UUID (repeatable)")
        if action in {"files", "read"}: item.add_argument("path", nargs="?", default="")
        if action == "preview":
            item.add_argument("path", help="Remote HTML entry file")
            item.add_argument("--asset", help="Workspace-relative resource, rather than the entry HTML")
            item.add_argument("--offset", type=int, help="Read a bounded base64 content chunk at this offset")
            item.add_argument("--length", type=int, default=262144, help="Chunk bytes, at most 524288")


async def dispatch(args, emit):
    if args.action in {"references", "forget"}:
        from xuloom.remote import navigation
        return {"ok": True, "reason": "completed", **({"references": navigation.references()} if args.action == "references" else navigation.forget(args.id, args.version, confirm=args.confirm))}
    if args.action == "chat": raise ValueError("Use the interactive remote chat entry point; remote exec provides JSONL execution")
    if args.action == "list": return {"ok": True, "reason": "completed", "connections": config.profiles()}
    if args.action == "add":
        value = config.save({key: getattr(args, key) for key in ("id", "name", "host", "port", "cwd", "python", "platform", "ssh_config")})
        return {"ok": True, "reason": "completed", "connection": value}
    if args.action == "remove":
        config.remove(args.id); return {"ok": True, "reason": "completed", "removed": args.id}
    if args.action in {"inspect", "summary", "attachments", "download"} and not args.session: raise ValueError("--session is required")
    if args.action == "exec":
        if args.resume and not args.session: raise ValueError("--resume requires --session")
        if args.resume and args.prompt: raise ValueError("--resume cannot replace the saved prompt")
        if args.resume and (args.attach or args.attachment): raise ValueError("--resume cannot replace the saved attachments")
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
        elif args.action == "preview":
            if session_id: await client.request("initialize", {"sessionId": session_id})
            params = {"path": args.path, "asset": args.asset}
            file = await client.request("files.htmlInfo", params)
            chunk = {} if args.offset is None else {"chunk": await client.request("files.htmlChunk",
                {**params, "asset": file["asset"], "version": file["version"], "offset": args.offset, "length": args.length})}
            result = {"ok": True, "reason": "completed", "file": file, **chunk}
        elif args.action == "summary":
            await client.request("initialize", {"sessionId": session_id})
            result = {"ok": True, "reason": "completed", "summary": await client.request("session.summary", {"turnId": args.turn, "all": args.all})}
        elif args.action == "inspect":
            initial = await client.request("initialize", {"sessionId": session_id})
            status = await client.request("session.status", {"sessionId": session_id})
            result = {"ok": True, "reason": "completed", "history": initial["history"], "status": status}
        elif args.action in {"attach", "attachments", "download"}:
            from xuloom.remote import attachments
            attachments.supported(client)
            if not session_id: session_id = (await client.request("sessions.create"))["sessionId"]
            await client.request("initialize", {"sessionId": session_id})
            await emit({"type": "session_started", "session_id": session_id, "remote": {"id": args.id, "host": client.profile["host"]}})
            if args.action == "attach":
                value = await attachments.upload(client, [{"path": path} for path in args.files], emit=emit)
            elif args.action == "attachments": value = await client.request("attachments.list", {"cursor": args.cursor})
            else: value = await attachments.download(client, args.attachment, args.destination)
            result = {"ok": True, "reason": "completed", "attachments" if args.action != "download" else "file": value}
        else:
            if not session_id: session_id = (await client.request("sessions.create"))["sessionId"]
            await client.request("initialize", {"sessionId": session_id, "permissionMode": args.permission})
            # Persist the target identity in the caller's event stream before
            # asking for any effect. Never automatically retry prompt RPCs.
            await emit({"type": "session_started", "session_id": session_id, "remote": {"id": args.id, "host": client.profile["host"]}})
            attachment_ids = list(args.attachment)
            if args.attach or attachment_ids:
                from xuloom.remote import attachments
                attachments.supported(client)
                if args.attach:
                    items = await attachments.upload(client, [{"path": path} for path in args.attach], existing=attachment_ids, emit=emit)
                else: items = (await client.request("attachments.list", {"ids": attachment_ids}))["items"]
                if not all(item.get("available") for item in items): raise ValueError("Remove or re-upload unfinished attachments before submitting")
                attachment_ids = [item["id"] for item in items]
            if args.collaboration: await client.request("collaboration.set", {"mode": args.collaboration})
            await client.request("prompt.resume" if args.resume else "prompt", {} if args.resume else {"text": text, "skills": args.skill, "attachments": attachment_ids})
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
