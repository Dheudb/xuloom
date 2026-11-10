"""Bounded attachment publication; unfinished transfers never become model input."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from xuloom.core.storage import atomic_json
from xuloom.desktop.attachments import attachment_directory, attachment_item, jpeg_image, IMAGE_EXTENSIONS
from xuloom.features.media_preview import version, read_range

CHUNK = 262144


def begin(cwd, session_id, identity, name, size):
    if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
        raise ValueError("Invalid attachment ID")
    if not isinstance(name, str) or not name or len(name) > 512 or any(c in name for c in "\0/\\:"):
        raise ValueError("Attachment name must be a filename")
    name.encode("utf-8")
    extension = Path(name).suffix.lower()
    kind = "image" if extension in IMAGE_EXTENSIONS else "file"
    if type(size) is not int or not 0 <= size <= (20 if kind == "image" else 64) * 1024 * 1024:
        raise ValueError("Attachment exceeds the image/file size limit")
    extension = extension if re.fullmatch(r"\.[a-z0-9_-]{1,19}", extension) else ".bin"
    directory = attachment_directory(cwd, session_id, create=True)
    file = directory / (identity + extension)
    manifest, part = directory / (identity + ".json"), directory / (identity + ".part")
    if any(p.exists() or p.is_symlink() for p in (file, manifest, part)):
        raise ValueError("Attachment ID already exists; inspect its state instead of replaying")
    raw = {"id": identity, "file": file.name, "name": name, "size": size, "kind": kind, "state": "uploading", "preview": ""}
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(raw, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
    with part.open("xb"): pass
    return {"cwd": cwd, "session_id": session_id, "raw": raw, "part": part, "file": file, "manifest": manifest, "published": False, "created_final": False}


def append(upload, offset, data):
    attachment_directory(upload["cwd"], upload["session_id"])
    if type(offset) is not int or offset < 0 or not isinstance(data, str) or len(data) > (CHUNK + 2) // 3 * 4:
        raise ValueError("Invalid attachment chunk")
    value = base64.b64decode(data, validate=True)
    if not value or len(value) > CHUNK or base64.b64encode(value).decode("ascii") != data:
        raise ValueError("Invalid attachment chunk")
    if upload["part"].is_symlink(): raise ValueError("Linked upload is not supported")
    with upload["part"].open("r+b") as stream:
        if os.fstat(stream.fileno()).st_size != offset or offset + len(value) > upload["raw"]["size"]:
            raise ValueError("Upload offset/size does not match; no chunk was replayed")
        stream.seek(offset); stream.write(value); stream.flush()
    return {"id": upload["raw"]["id"], "offset": offset, "nextOffset": offset + len(value)}


def commit(upload, digest):
    attachment_directory(upload["cwd"], upload["session_id"])
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest): raise ValueError("Invalid attachment digest")
    if upload["part"].is_symlink(): raise ValueError("Linked upload is not supported")
    actual = hashlib.sha256()
    with upload["part"].open("rb") as source:
        if os.fstat(source.fileno()).st_size != upload["raw"]["size"]: raise ValueError("Upload is incomplete")
        for chunk in iter(lambda: source.read(1024 * 1024), b""): actual.update(chunk)
    if actual.hexdigest() != digest: raise ValueError("Attachment digest does not match")
    thumbnail = "data:image/jpeg;base64," + jpeg_image(upload["part"], (112, 80)) if upload["raw"]["kind"] == "image" else ""
    with upload["part"].open("rb") as source, upload["file"].open("xb") as destination:
        upload["created_final"] = True
        shutil.copyfileobj(source, destination, 1024 * 1024); destination.flush(); os.fsync(destination.fileno())
    with upload["file"].open("rb") as stream: stamp = version(upload["file"].resolve(), os.fstat(stream.fileno()))
    raw = {**upload["raw"], "state": "ready", "preview": thumbnail, "sha256": digest, "version": stamp}
    atomic_json(upload["manifest"], raw)
    upload["published"] = True
    upload["part"].unlink()
    return attachment_item(upload["cwd"], upload["session_id"], raw["id"])


def abort(upload):
    attachment_directory(upload["cwd"], upload["session_id"])
    if upload["part"].is_symlink(): raise ValueError("Linked upload is not supported")
    if upload["part"].exists(): upload["part"].unlink()
    if not upload["published"]:
        if upload["created_final"] and upload["file"].exists(): upload["file"].unlink()
        atomic_json(upload["manifest"], {**upload["raw"], "state": "interrupted"})


def listing(cwd, session_id, ids=None, cursor=""):
    more = False
    if not isinstance(cursor, str): raise ValueError("Invalid attachment cursor")
    if ids is None:
        directory = attachment_directory(cwd, session_id)
        names = sorted(p.stem for p in directory.glob("*.json") if re.fullmatch(r"[a-f0-9-]{36}", p.stem) and p.stem > cursor)
        ids, more = names[:100], len(names) > 100
    if not isinstance(ids, list) or len(ids) > 100 or not all(isinstance(i, str) for i in ids): raise ValueError("Invalid attachment list")
    items = [attachment_item(cwd, session_id, identity, pending=True) for identity in dict.fromkeys(ids)]
    return {"items": items, "nextCursor": ids[-1] if more else None}


def info(cwd, session_id, identity):
    item = attachment_item(cwd, session_id, identity)
    if not item.get("sha256"):
        digest = hashlib.sha256()
        with Path(item["path"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
        item["sha256"] = digest.hexdigest()
    return item


def read(cwd, session_id, identity, stamp, offset, length):
    item = attachment_item(cwd, session_id, identity)
    return read_range(Path(item["path"]).resolve(), stamp, offset, length, 64 * 1024 * 1024)


async def io(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def cancel(bridge):
    async with bridge.attachment_guard:
        await _finish(bridge)


async def _finish(bridge):
    current = bridge.attachment_upload
    if current is None: return
    upload, manager = current
    try: await io(abort, upload)
    finally:
        bridge.attachment_upload = None
        if manager: manager.release_writer(upload["session_id"])


async def dispatch(bridge, method, params):
    sid = bridge.engine.session_id if bridge.engine else bridge.resume_id
    if not sid: raise ValueError("Open a saved session before transferring attachments")
    if method == "attachments.list": return await io(listing, bridge.cwd, sid, params.get("ids"), params.get("cursor", ""))
    if method == "attachments.info": return await io(info, bridge.cwd, sid, params.get("id"))
    if method == "attachments.read": return await io(read, bridge.cwd, sid, params.get("id"), params.get("version"), params.get("offset", 0), params.get("length", CHUNK))
    if method == "attachments.preview":
        from xuloom.desktop.attachments import image_preview
        return await io(image_preview, bridge.cwd, sid, params.get("id"))
    if method not in {"attachments.begin", "attachments.write", "attachments.commit", "attachments.abort"}:
        raise ValueError("Unsupported attachment operation")
    async with bridge.attachment_guard:
        if method == "attachments.begin" and bridge.attachment_upload is not None:
            raise ValueError("An attachment upload is already running")
        if method != "attachments.begin" and (bridge.attachment_upload is None or
                bridge.attachment_upload[0]["session_id"] != sid or bridge.attachment_upload[0]["raw"]["id"] != params.get("id")):
            raise ValueError("Attachment upload is not owned by this connection")
        manager = None
        try:
            if method == "attachments.begin":
                bridge.require_idle()
                from xuloom.core.session import SessionManager
                with SessionManager(cwd=str(bridge.cwd)) as reader:
                    state = reader.load_session_state(sid)
                if not state.get("_valid") or state.get("metadata", {}).get("delegation", {}).get("authority_required"):
                    raise ValueError("Missing, invalid or controlled session; attachments cannot modify this history")
                if bridge.engine:
                    owned = bridge.engine.session_manager.owns_writer(sid)
                    bridge.engine.require_writer()
                    if not owned: manager = bridge.engine.session_manager
                else:
                    manager = SessionManager(cwd=str(bridge.cwd)); manager.acquire_writer(sid)
                with SessionManager(cwd=str(bridge.cwd)) as reader:
                    state = reader.load_session_state(sid)
                if not state.get("_valid") or state.get("metadata", {}).get("delegation", {}).get("authority_required"):
                    raise ValueError("Session authority changed before attachment upload")
                def start():
                    value = begin(bridge.cwd, sid, params.get("id"), params.get("name"), params.get("size"))
                    bridge.attachment_upload = (value, manager)
                    return {"id": value["raw"]["id"], "state": "uploading", "offset": 0}
                return await io(start)
            current = bridge.attachment_upload
            if current is None or current[0]["session_id"] != sid or current[0]["raw"]["id"] != params.get("id"):
                raise ValueError("Attachment upload is not owned by this connection")
            if method == "attachments.write": return await io(append, current[0], params.get("offset"), params.get("data"))
            result = await io(commit, current[0], params.get("sha256")) if method == "attachments.commit" else {"id": params["id"], "state": "interrupted"}
            await _finish(bridge)
            return result
        except BaseException:
            if method == "attachments.begin": await _finish(bridge)
            if manager: manager.release_writer(sid)
            raise
