"""Content-bound published versions; paging verifies only the requested blocks."""
import base64
from functools import lru_cache
import hashlib
import json
import os
import re

from xuloom.core.storage import IOPath, atomic_bytes, atomic_copy, file_hash

PAGE = 24000
MAX_BYTES = 32 * 1024 * 1024


def cache_key(run,checksum):return hashlib.sha256((run+":"+checksum).encode()).hexdigest()


def no_links(path, root):
    probe=path
    while True:
        if probe.is_symlink() or (probe.exists() and getattr(probe.lstat(),"st_file_attributes",0)&0x400):
            raise ValueError("Shared artifacts cannot traverse links")
        if probe==root:break
        probe=probe.parent


def workspace_file(row,path):
    root=IOPath(os.path.abspath(row["cwd"]));target=IOPath(os.path.abspath(str(root/path)))
    if not target.is_relative_to(root):raise ValueError("Publish artifacts from this worker's own workspace")
    no_links(target,root)
    if not target.is_file():raise ValueError("Artifact is not a file")
    if target.stat().st_size>MAX_BYTES:raise ValueError("Shared artifact exceeds 32 MiB")
    return root,target


def file_info(node,row,path):
    root,target=workspace_file(row,path);before=target.stat();checksum=file_hash(target);after=target.stat()
    if (before.st_mtime_ns,before.st_size,before.st_ino)!=(after.st_mtime_ns,after.st_size,after.st_ino):raise ValueError("Artifact changed while hashing")
    return {"path":target.relative_to(root).as_posix(),"bytes":after.st_size,"sha256":checksum}


def paths(node,run,checksum):
    if not isinstance(checksum,str) or not re.fullmatch(r"[a-f0-9]{64}",checksum):raise ValueError("Invalid artifact hash")
    root=IOPath(node.store.path.parent)/"shared-artifacts"/hashlib.sha256(run.encode()).hexdigest()
    blob=root/(checksum+".blob");index=root/(checksum+".json")
    no_links(blob,IOPath(node.store.path.parent));no_links(index,IOPath(node.store.path.parent))
    return blob,index


@lru_cache(maxsize=64)
def load_index(path,expected):
    with IOPath(path).open("rb") as handle:raw=handle.read(200001)
    if len(raw)>200000 or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError("Artifact index changed")
    data=json.loads(raw)
    if (not isinstance(data,dict) or data.get("version")!=1 or data.get("page")!=PAGE or type(data.get("bytes")) is not int
        or not 0<=data["bytes"]<=MAX_BYTES or not isinstance(data.get("chunks"),list)
        or len(data["chunks"])!=(data["bytes"]+PAGE-1)//PAGE
        or any(not isinstance(h,str) or not re.fullmatch(r"[a-f0-9]{64}",h) for h in data["chunks"])):
        raise ValueError("Invalid artifact index")
    return data


def snapshot(node,row,info,run):
    blob,index=paths(node,run,info["sha256"])
    key=cache_key(run,info["sha256"])
    try:cached=node.store.get("artifact_blob",key)
    except ValueError:
        _,source=workspace_file(row,info["path"])
        atomic_copy(blob,source,info["sha256"])
        chunks=[];checksum=hashlib.sha256()
        with blob.open("rb") as handle:
            for chunk in iter(lambda:handle.read(PAGE),b""):
                chunks.append(hashlib.sha256(chunk).hexdigest());checksum.update(chunk)
        if checksum.hexdigest()!=info["sha256"]:raise ValueError("Artifact snapshot changed during publication")
        raw=json.dumps({"version":1,"page":PAGE,"sha256":info["sha256"],"bytes":info["bytes"],"chunks":chunks},separators=(",",":")).encode()
        atomic_bytes(index,raw)
        cached=node.store.put("artifact_blob",{"id":key,"run_id":run,"index_sha256":hashlib.sha256(raw).hexdigest()})
    data=load_index(str(index),cached["index_sha256"])
    if data["sha256"]!=info["sha256"] or data["bytes"]!=info["bytes"]:raise ValueError("Artifact snapshot identity changed")
    return {**info,"snapshot":1,"index_sha256":cached["index_sha256"]}


def publish(node,row,path,run):
    return snapshot(node,row,file_info(node,row,path),run)


def read(node,row,record,offset):
    size=record.get("bytes")
    if type(size) is not int or not 0<=size<=MAX_BYTES:raise ValueError("Invalid artifact size")
    if type(offset) is not int or not 0<=offset<=size:raise ValueError("Invalid artifact offset")
    blob,index=paths(node,record["run_id"],record.get("sha256"))
    if record.get("snapshot") is not None and (type(record["snapshot"]) is not int or record["snapshot"]!=1):raise ValueError("Unsupported artifact snapshot version")
    if record.get("snapshot") is None:
        # Upgrade an old manifest once, and only from matching original bytes.
        try:cached=node.store.get("artifact_blob",cache_key(record["run_id"],record["sha256"]))
        except ValueError:
            info=file_info(node,row,record["path"])
            if (info["sha256"],info["bytes"])!=(record["sha256"],size):raise ValueError("Original artifact changed before archival")
            cached=snapshot(node,row,info,record["run_id"])
        record={**record,"snapshot":1,"index_sha256":cached["index_sha256"]}
    proof=record.get("index_sha256")
    if not isinstance(proof,str) or not re.fullmatch(r"[a-f0-9]{64}",proof):raise ValueError("Invalid artifact index hash")
    data=load_index(str(index),proof)
    if data["sha256"]!=record["sha256"] or data["bytes"]!=size:raise ValueError("Foreign artifact index")
    start=offset//PAGE;end=min(size,offset+PAGE);part=b""
    with blob.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size!=size:raise ValueError("Artifact snapshot changed")
        handle.seek(start*PAGE)
        for number in range(start,(end+PAGE-1)//PAGE):
            chunk=handle.read(min(PAGE,size-number*PAGE))
            if hashlib.sha256(chunk).hexdigest()!=data["chunks"][number]:raise ValueError("Artifact snapshot changed")
            part+=chunk
    part=part[offset-start*PAGE:offset-start*PAGE+end-offset]
    return {"entry":record["id"],"text":part.decode("utf-8",errors="replace"),"base64":base64.b64encode(part).decode(),
        "offset":offset,"next_offset":end if end<size else None,"sha256":record["sha256"],"total_bytes":size}
