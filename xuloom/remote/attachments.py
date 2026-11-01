"""Local file transfer clients shared by desktop SSH and the formal CLI."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

from xuloom.desktop.attachment_transfer import CHUNK, io
from xuloom.desktop.attachments import IMAGE_EXTENSIONS


def supported(client):
    if client.info.get("attachments") != 1: raise ValueError("Update remote Xuloom to use attachment transfers")


def check_cancelled(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise ValueError("Attachment transfer cancelled; incomplete IDs were retained for inspection")


def stamp(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino


def source_file(value, identity):
    if not isinstance(value, dict): raise ValueError("Invalid attachment input")
    if value.get("path"):
        path = Path(value["path"]).expanduser().resolve(strict=True)
        if not path.is_file(): raise ValueError("Attach a file, not a directory")
        source = path.open("rb")
        return source, path.name, os.fstat(source.fileno())
    data = value.get("data")
    if not isinstance(data, str) or len(data) > (20 * 1024 * 1024 + 2) // 3 * 4 + 100: raise ValueError("Pasted image exceeds 20 MB")
    match = re.fullmatch(r"data:image/(png|jpeg|webp|gif|bmp);base64,([A-Za-z0-9+/]+={0,2})", data)
    if not match: raise ValueError("Invalid pasted image")
    content = base64.b64decode(match[2], validate=True)
    if base64.b64encode(content).decode("ascii") != match[2]: raise ValueError("Invalid pasted image")
    import io as buffers
    extension = "jpg" if match[1] == "jpeg" else match[1]
    return buffers.BytesIO(content), "粘贴图片-" + identity[:8] + "." + extension, None


async def upload(client, files, identities=None, existing=(), emit=None, cancelled=None):
    supported(client)
    if not isinstance(files, list) or not files or not isinstance(existing, (list, tuple)) or not all(isinstance(i, str) for i in existing) or len(files) + len(existing) > 10:
        raise ValueError("Each message supports at most 10 attachments")
    identities = identities if identities is not None else [str(uuid.uuid4()) for _ in files]
    if not isinstance(identities, list) or not all(isinstance(i, str) for i in identities) or len(identities) != len(files) or len(set(identities)) != len(identities):
        raise ValueError("Invalid attachment identities")
    if len(set([*existing, *identities])) != len(existing) + len(identities): raise ValueError("Attachment identities must be distinct")
    for identity in [*existing, *identities]:
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity: raise ValueError("Invalid attachment identity")
    await client.request("attachments.list", {"ids": list(existing)})
    for value, identity in zip(files, identities):
        opened, begun = [], False
        try:
            check_cancelled(cancelled)
            def opening():
                result = source_file(value, identity); opened.append(result[0]); return result
            source, name, before = await io(opening)
            size = before.st_size if before is not None else source.getbuffer().nbytes
            if size > (20 if Path(name).suffix.lower() in IMAGE_EXTENSIONS else 64) * 1024 * 1024:
                raise ValueError("Attachment exceeds the image/file size limit")
            if emit: await emit({"type": "attachment_started", "id": identity, "name": name, "size": size})
            check_cancelled(cancelled)
            result = await client.request("attachments.begin", {"id": identity, "name": name, "size": size})
            begun = True
            if result.get("id") != identity or result.get("offset") != 0 or result.get("state") != "uploading": raise ValueError("Invalid upload receipt")
            offset, digest = 0, hashlib.sha256()
            while content := await io(source.read, CHUNK):
                check_cancelled(cancelled)
                digest.update(content)
                result = await client.request("attachments.write", {"id": identity, "offset": offset, "data": base64.b64encode(content).decode("ascii")})
                if result.get("id") != identity or result.get("offset") != offset or result.get("nextOffset") != offset + len(content):
                    raise ValueError("Invalid upload offset receipt; no chunk was replayed")
                offset += len(content)
            if offset != size or before is not None and stamp(before) != stamp(os.fstat(source.fileno())):
                raise ValueError("Local attachment changed while uploading")
            check_cancelled(cancelled)
            result = await client.request("attachments.commit", {"id": identity, "sha256": digest.hexdigest()})
            if result.get("id") != identity or not result.get("available") or result.get("size") != size or result.get("sha256") != digest.hexdigest():
                raise ValueError("Attachment completion was not confirmed; reopen the session to inspect its saved ID")
        except BaseException:
            if begun:
                try: await asyncio.shield(client.request("attachments.abort", {"id": identity}, timeout=10))
                except (ConnectionError, ValueError, OSError, asyncio.CancelledError): pass
            raise
        finally:
            for source in opened: source.close()
    return (await client.request("attachments.list", {"ids": list(existing) + identities}))["items"]


async def download(client, identity, destination, cancelled=None):
    supported(client)
    check_cancelled(cancelled)
    item = await client.request("attachments.info", {"id": identity})
    if item.get("id") != identity or not item.get("available") or type(item.get("size")) is not int or not 0 <= item["size"] <= 64 * 1024 * 1024 or not re.fullmatch("[a-f0-9]{64}", item.get("sha256", "")) or not re.fullmatch("[a-f0-9]{64}", item.get("version", "")) or not isinstance(item.get("name"), str):
        raise ValueError("Invalid attachment download metadata")
    target = Path(destination).expanduser().absolute()
    if not target.parent.is_dir() or target.is_dir(): raise ValueError("Choose a destination filename in an existing directory")
    fd, temporary = tempfile.mkstemp(prefix=".xuloom-download-", dir=target.parent)
    try:
        offset, digest = 0, hashlib.sha256()
        with os.fdopen(fd, "wb") as stream:
            while offset < item["size"]:
                check_cancelled(cancelled)
                result = await client.request("attachments.read", {"id": identity, "version": item["version"], "offset": offset, "length": CHUNK})
                if not isinstance(result.get("data"), str) or len(result["data"]) > (CHUNK + 2) // 3 * 4: raise ValueError("Invalid attachment chunk")
                value = base64.b64decode(result["data"], validate=True)
                if not value or len(value) > min(CHUNK, item["size"] - offset) or result.get("offset") != offset or result.get("nextOffset") != offset + len(value) or result.get("size") != item["size"] or result.get("version") != item["version"]:
                    raise ValueError("Invalid attachment chunk receipt")
                stream.write(value); digest.update(value); offset += len(value)
            stream.flush(); os.fsync(stream.fileno())
        if digest.hexdigest() != item["sha256"]: raise ValueError("Attachment download digest does not match")
        check_cancelled(cancelled)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {"id": identity, "name": item["name"], "path": str(target), "size": item["size"], "sha256": item["sha256"]}
