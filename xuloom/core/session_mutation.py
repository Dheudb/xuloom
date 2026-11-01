"""Session-owned storage commits. The process holding the OS lease performs every mutation.

Intent is durable before history changes. Interrupted internal copies/removals finish from
verified bytes before another writer proceeds; this never replays a model tool or command.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from xuloom.branding import configuration_file
import re
import stat
import time
import uuid

from xuloom.core.session_lock import SessionLock, SessionConflictError, SessionBusyError
from xuloom.core.storage import data_directory, history_root, atomic_json, atomic_bytes, atomic_copy, file_hash, rebase


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _safe(path):
    path = Path(os.path.abspath(path))
    configured = Path(os.path.abspath(data_directory() / "history"))
    if configured.exists():
        info = configured.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("The history directory cannot be a link or junction")
    base = configured.resolve()
    parts = None
    for candidate in (configured, base):
        try:
            parts = path.relative_to(candidate).parts
            break
        except ValueError:
            continue
    if parts is None:
        raise ValueError("Session storage must stay in central history") from None
    if not parts:
        raise ValueError("Cannot mutate the history root")
    probe = base
    for part in parts:
        probe /= part
        try:
            info = probe.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Session resources cannot be links or junctions")
    return probe


def _files(directory):
    directory = _safe(directory)
    if not directory.exists():
        return []
    if directory.is_file():
        return [directory]
    return [file for child in sorted(directory.iterdir()) for file in _files(child)]


def _index(root):
    path = _safe(root / "index.json")
    data = json.loads(path.read_bytes()) if path.exists() else {"version": 1, "sessions": {}}
    if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
        raise ValueError("Invalid session index; existing records were preserved")
    return data


def _workspace(base, target, identity, seed=None):
    SessionLock(data_directory() / "session-locks", identity)
    base, target = Path(os.path.realpath(base)), Path(os.path.realpath(target))
    if not base.is_dir() or target != base / ("chat-" + identity) or target.exists():
        raise ValueError("Independent conversations require a new private workspace")
    base = Path(os.path.realpath(seed or base))
    files = {}
    settings = configuration_file(base, "capabilities.json")
    inherited = json.loads(settings.read_bytes()) if settings.exists() else {}
    if not isinstance(inherited, dict):
        raise ValueError("Invalid workspace capability configuration")
    roots = {kind: list(dict.fromkeys(inherited.get(key, []) + [str(base / name / kind)
        for name in (".xuloom", ".agents", ".claude") if (base / name / kind).is_dir()]))
        for kind, key in (("skills", "skill_roots"), ("agents", "agent_roots"))}
    if settings.exists() or any(roots.values()):
        files[".xuloom/capabilities.json"] = json.dumps({**inherited, "skill_roots": roots["skills"], "agent_roots": roots["agents"]}, ensure_ascii=False, indent=2)
    mcp = configuration_file(base, "mcp_servers.json")
    if mcp.is_file() and not mcp.is_symlink() and not getattr(mcp.lstat(), "st_file_attributes", 0) & 0x400:
        files[mcp.relative_to(base).as_posix()] = mcp.read_text(encoding="utf-8")
    return {"target": str(target), "files": files}


def _workspace_path(root, relative):
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SessionConflictError("Invalid workspace output path")
    for probe in (root, *(root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1))):
        if probe.is_symlink() or probe.exists() and getattr(probe.lstat(), "st_file_attributes", 0) & 0x400:
            raise SessionConflictError("Workspace files cannot be links or junctions")
    return root / relative


def _legacy_outputs(project, checkpoint):
    """Identify only workspace-local outputs referenced by the old execution record."""
    names = set()
    def collect(value):
        if isinstance(value, dict):
            arguments = value.get("input", {})
            if value.get("type") == "tool_use" and value.get("name") in {"file_write_tool", "file_edit_tool", "notebook_edit_tool"} and isinstance(arguments, dict):
                name = arguments.get("file_path") or arguments.get("notebook_path")
                if isinstance(name, str): names.add(name)
            if value.get("operation") and "added" in value and isinstance(value.get("path"), str): names.add(value["path"])
            for item in value.values(): collect(item)
        elif isinstance(value, list):
            for item in value: collect(item)
    collect(checkpoint)
    project, files = Path(project).resolve(), {}
    for name in sorted(names):
        source = Path(os.path.abspath(project / name))
        try:
            relative = source.relative_to(project)
            source = _workspace_path(project, relative)
        except (ValueError, SessionConflictError):
            continue
        if not relative.parts or any(part.startswith(".") for part in relative.parts) or not source.is_file() or source.is_symlink() or getattr(source.lstat(), "st_file_attributes", 0) & 0x400:
            continue
        files[relative.as_posix()] = source
    return files


def _paths(project, sid):
    SessionLock(data_directory() / "session-locks", sid)  # Validate before constructing paths.
    root = history_root(project)
    return root, _safe(root / "sessions" / (sid + ".json")), _safe(root / "tool-results" / sid)


def _pointer(identity):
    SessionLock(data_directory() / "session-locks", identity)
    return data_directory() / "session-operations" / (identity + ".json")


def _clean(journal, path):
    for identity in journal["identities"]:
        pointer = _pointer(identity)
        if pointer.exists() and json.loads(pointer.read_bytes()).get("transaction") == path.stem[len("transaction-"):]:
            pointer.unlink()
    path.unlink(missing_ok=True)


def _apply(journal):
    for workspace in journal.get("workspaces", []):
        target = Path(workspace["target"])
        if target.is_symlink() or target.exists() and getattr(target.lstat(), "st_file_attributes", 0) & 0x400:
            raise SessionConflictError("Workspace cannot be a link or junction")
        target.mkdir(exist_ok=True)
        for relative, text in workspace["files"].items():
            path = _workspace_path(target, relative)
            content = text.encode("utf-8")
            if path.exists():
                if path.read_bytes() != content:
                    raise SessionConflictError("Workspace configuration changed during recovery; user files were preserved")
            else:
                atomic_bytes(path, content)
    # Copy all owned output before publishing the destination checkpoint (last copy).
    for entry in journal["copies"]:
        workspace = entry.get("workspace")
        if workspace:
            source = _workspace_path(workspace[0], Path(entry["source"]).relative_to(workspace[0]))
            target = _workspace_path(workspace[1], Path(entry["target"]).relative_to(workspace[1]))
        else:
            source = _safe(entry["source"]) if entry["source"] is not None else None
            target = _safe(entry["target"])
        content = entry["content"].encode("utf-8") if entry.get("content") is not None else None
        if target.exists():
            if file_hash(target) != entry["target_hash"]:
                raise SessionConflictError("Destination changed during a session operation; both records were preserved")
            continue
        if workspace:
            atomic_copy(target, source, entry["source_hash"])
            os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
            continue
        original = source.read_bytes() if source is not None else b""
        if _hash(original) != entry["source_hash"]:
            raise SessionConflictError("Source changed during a session operation; records were preserved")
        atomic_bytes(target, original if content is None else content)
        os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
    for entry in journal["removes"]:
        path = _safe(entry["path"])
        if path.exists():
            if _hash(path.read_bytes()) != entry["hash"]:
                raise SessionConflictError("Session resource changed before removal; it was preserved")
            try:
                path.unlink()
            except PermissionError:
                # A read-only owned copy may be removed, but an open Windows handle remains an error.
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
                try:
                    path.unlink()
                except PermissionError as error:
                    raise PermissionError("会话附件仍被占用，未完成的会话记录已保留；关闭相关文件后重试。") from error
    for entry in journal["indexes"]:
        path = _safe(entry["path"])
        data = _index(path.parent)
        current = data["sessions"].get(entry["session_id"])
        if current == entry["after"]:
            continue
        if current != entry["before"]:
            raise SessionConflictError("Session metadata changed during storage commit; inspect the pending operation")
        if entry["after"] is None:
            data["sessions"].pop(entry["session_id"], None)
        else:
            data["sessions"][entry["session_id"]] = entry["after"]
        atomic_json(path, data)
    for directory in journal["directories"]:
        path = _safe(directory)
        if path.is_dir():
            for current, _, _ in os.walk(path, topdown=False, followlinks=False):
                try:
                    _safe(current).rmdir()
                except OSError:
                    pass  # Never remove an unexpected non-empty directory.


def recover_pending(identity, held=()):
    pointer = _pointer(identity)
    if not pointer.exists():
        return
    transaction = json.loads(pointer.read_bytes()).get("transaction", "")
    if not re.fullmatch(r"[a-f0-9]{32}", transaction):
        raise SessionConflictError("Invalid pending session operation; history was preserved")
    path = pointer.parent / ("transaction-" + transaction + ".json")
    journal = json.loads(path.read_bytes())
    if identity not in journal["identities"]:
        raise SessionConflictError("Pending operation does not own this session")
    return _recover_journal(path, held)


def _recover_journal(path, held=()):
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise SessionConflictError("Pending operations cannot be links or junctions")
    journal = json.loads(path.read_bytes())
    with ExitStack() as stack:
        for sid in sorted(set(journal["identities"]) - set(held)):
            lock = SessionLock(data_directory() / "session-locks", sid)
            lock.acquire(); stack.callback(lock.close)
        # A competing recovery may have finished while these leases were acquired.
        if not path.exists():
            return
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise SessionConflictError("Pending operations cannot be links or junctions")
        current = json.loads(path.read_bytes())
        if current["identities"] != journal["identities"]:
            raise SessionConflictError("Pending operation ownership changed during recovery")
        journal = current
        if journal["phase"] == "committing":
            _apply(journal)
            journal["phase"] = "completed"
            atomic_json(path, journal)
        elif journal["phase"] not in {"prepared", "completed"}:
            raise SessionConflictError("Unknown session operation phase")
        recovered = journal.get("request_hash") if journal["phase"] == "completed" else None
        _clean(journal, path)
        return recovered


def recover_storage():
    recovered, busy = 0, []
    for path in sorted((data_directory() / "session-operations").glob("transaction-*.json")):
        try:
            _recover_journal(path)
            recovered += 1
        except FileNotFoundError:
            if path.exists():
                raise  # A missing copy source is an error, not a successful recovery.
        except SessionBusyError:
            busy.append(path.name)
    return {"completed": not busy, "recovered": recovered, "busy": busy}


def _plan(spec, identities):
    plan = {"identities": identities, "copies": [], "removes": [], "indexes": [], "directories": []}
    def copy(source, target, payload=None, workspace=None):
        if workspace:
            source = _workspace_path(workspace[0], Path(source).relative_to(workspace[0]))
            target = _workspace_path(workspace[1], Path(target).relative_to(workspace[1]))
        else:
            source, target = _safe(source) if source is not None else None, _safe(target)
        if target.exists():
            raise ValueError("目标项目存在同名会话资源，原记录保留")
        original = source.read_bytes() if source is not None and not workspace else b""
        source_hash = file_hash(source) if workspace else _hash(original)
        text = json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else None
        plan["copies"].append({"source": str(source) if source is not None else None, "target": str(target), "source_hash": source_hash,
            "target_hash": _hash(text.encode("utf-8")) if text is not None else source_hash, "content": text,
            **({"workspace": [str(root) for root in workspace]} if workspace else {}),
            "mtime_ns": source.stat().st_mtime_ns if source is not None else time.time_ns()})
    def remove(path):
        path = _safe(path)
        if path.exists():
            plan["removes"].append({"path": str(path), "hash": _hash(path.read_bytes())})
    def index(root, sid, after):
        before = _index(root)["sessions"].get(sid)
        plan["indexes"].append({"path": str(root / "index.json"), "session_id": sid, "before": before, "after": after})
    if spec["action"] == "create":
        root, checkpoint, _ = _paths(spec["project"], spec["session_id"])
        if checkpoint.exists() or spec["session_id"] in _index(root)["sessions"]:
            raise SessionConflictError("Session identity already exists; existing history was preserved")
        if not isinstance(spec.get("messages", []), list) or spec.get("scope", "project") not in {"project", "standalone"}:
            raise ValueError("Invalid new conversation")
        if spec.get("workspace_base") is not None:
            if spec.get("scope") != "standalone": raise ValueError("Only independent conversations create workspaces")
            plan["workspaces"] = [_workspace(spec["workspace_base"], spec["project"], spec["session_id"])]
        copy(None, checkpoint, {"messages": spec.get("messages", []), "metadata": {}, "revision": 1})
        index(root, spec["session_id"], {"created": spec["created"], "scope": spec.get("scope", "project"),
            "workspaceIsolated": spec.get("scope") == "standalone", **({"title": spec["title"]} if spec.get("title") else {})})
        return plan
    if spec["action"] == "delete":
        for row in spec["sessions"]:
            root, checkpoint, artifacts = _paths(row["project"], row["session_id"])
            for file in _files(artifacts): remove(file)
            plan["directories"].append(str(artifacts))
            children = _safe(root / "sessions/subagents")
            if children.exists():
                for child in children.glob("*.json"):
                    try:
                        value = json.loads(_safe(child).read_bytes())
                        if isinstance(value, dict) and value.get("session_id") == row["session_id"]: remove(child)
                    except (ValueError, UnicodeError):
                        continue
            remove(checkpoint)  # An attachment denial leaves the conversation itself retryable.
            index(root, row["session_id"], None)
        return plan
    sid, project, target = spec["session_id"], os.path.realpath(spec["project"]), os.path.realpath(spec["target"])
    if spec["action"] == "fork" and spec.get("scope") == "standalone":
        plan["workspaces"] = [_workspace(Path(project).parent, target, spec["new_id"])]
    if spec.get("legacy_workspace") is not None:
        if spec["action"] != "move" or spec.get("scope") != "standalone":
            raise ValueError("Legacy isolation only applies to independent conversations")
        plan["workspaces"] = [_workspace(spec["legacy_workspace"]["base"], target, spec["legacy_workspace"]["id"], seed=project)]
    if not Path(target).is_dir() and not plan.get("workspaces"):
        raise ValueError("目标项目目录不存在")
    root, checkpoint, artifacts = _paths(project, sid)
    destination_id = spec.get("new_id", sid)
    destination_root, destination, target_artifacts = _paths(target, destination_id)
    raw = json.loads(checkpoint.read_bytes())
    if not (isinstance(raw, list) or isinstance(raw, dict) and isinstance(raw.get("messages"), list)):
        raise ValueError("Invalid session checkpoint")
    if checkpoint != destination and (destination.exists() or destination_id in _index(destination_root)["sessions"]):
        raise ValueError("目标项目中已有相同 ID 的会话，原记录保留")
    meta = dict(_index(root)["sessions"].get(sid, {}))
    if spec.get("legacy_workspace") is not None:
        if meta.get("scope") != "standalone" or meta.get("workspaceIsolated"):
            raise ValueError("Only a legacy independent conversation may be upgraded")
        for relative, source in _legacy_outputs(project, raw).items():
            copy(source, Path(target) / relative, workspace=(project, target))
    if spec["action"] == "fork":
        selected, fork_metadata = (raw if isinstance(raw, list) else raw["messages"]), {}
        if "through" in spec:
            from xuloom.core.session_branch import branch_snapshot
            selected, fork_metadata = branch_snapshot(raw, spec["through"], sid)
        payload = rebase({"messages": selected, "metadata": fork_metadata, "revision": 1},
                         root / "tool-results" / sid, destination_root / "tool-results" / destination_id)
        payload = rebase(payload, artifacts, target_artifacts)
        meta = {"title": spec["title"], "scope": spec.get("scope", "project"), "created": int(time.time() * 1000), "workspaceIsolated": spec.get("scope") == "standalone"}
    else:
        scope = spec.get("scope", "project")
        meta.update(scope=scope, title=spec["title"], created=spec["created"])
        if scope == "standalone": meta["workspaceIsolated"] = True
        if checkpoint == destination:
            index(root, sid, meta)
            return plan
        payload = rebase({"messages": raw, "metadata": {}} if isinstance(raw, list) else raw, root / "tool-results", destination_root / "tool-results")
        if scope == "standalone": payload = rebase(payload, project, target)
        metadata = payload.setdefault("metadata", {})
        references = metadata.get("artifacts", {}).get("references", {})
        for field in ("sandbox", "browser", "shell_tasks", "permissions", "memory_files", "file_state_cache", "artifacts", "incremental_write_queue", "auto_commit", "content_replacements", "security_classifier", "vcr", "team", "ultraplan", "memory_extractor", "hooks"):
            metadata.pop(field, None)
        if references:
            metadata["artifacts"] = {"references": references}
        metadata.setdefault("workspace_moves", []).append({"from": project, "to": target, "at": int(time.time() * 1000)})
        payload["revision"] = payload.get("revision", 0) + 1
        children = _safe(root / "sessions/subagents")
        owned = {row["agent_id"] for row in metadata.get("subagents", [])}
        if children.exists():
            for child in children.glob("*.json"):
                try:
                    value = json.loads(_safe(child).read_bytes())
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                if value.get("session_id") == sid or not value.get("session_id") and child.stem in owned:
                    copy(child, destination_root / "sessions/subagents" / child.name, rebase(value, root / "tool-results", destination_root / "tool-results"))
                    remove(child)
        index(root, sid, None)
    for file in _files(artifacts):
        copy(file, target_artifacts / file.relative_to(artifacts))
        if spec["action"] == "move": remove(file)
    copy(checkpoint, destination, payload)
    if spec["action"] == "move":
        remove(checkpoint)
        plan["directories"].append(str(artifacts))
    index(destination_root, destination_id, meta)
    return plan


def _metadata(spec):
    """Presentation fields merge under the index lease, independently of the engine writer."""
    root, checkpoint, _ = _paths(spec["project"], spec["session_id"])
    patch = spec.get("patch", {})
    if not isinstance(patch, dict) or patch.keys() - {"title", "pinned", "archived", "draft", "draftAttachments", "draftEditId", "permissionMode"}:
        raise ValueError("Invalid session metadata fields")
    if "title" in patch and (not isinstance(patch["title"], str) or not 1 <= len(patch["title"].strip()) <= 200):
        raise ValueError("会话名称需为 1–200 个字符")
    if any(field in patch and not isinstance(patch[field], bool) for field in ("pinned", "archived")):
        raise ValueError("Invalid session metadata flag")
    if "draft" in patch and not isinstance(patch["draft"], str):
        raise ValueError("Invalid session draft")
    if "permissionMode" in patch and patch["permissionMode"] not in {"ask", "full"}:
        raise ValueError("请选择请求批准或完全访问")
    if "draftEditId" in patch and (not isinstance(patch["draftEditId"], str) or patch["draftEditId"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", patch["draftEditId"])):
        raise ValueError("编辑目标无效")
    if "draftAttachments" in patch and (not isinstance(patch["draftAttachments"], list) or len(patch["draftAttachments"]) > 10 or
            any(not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity) for identity in patch["draftAttachments"])):
        raise ValueError("附件草稿无效")
    identity = "index_" + root.name
    lock = SessionLock(data_directory() / "session-locks", identity)
    for attempt in range(51):
        try:
            lock.acquire()
            break
        except SessionBusyError:
            if attempt == 50: raise
            time.sleep(0.01)
    try:
        recover_pending(identity, {identity})
        if not checkpoint.is_file():
            raise SessionConflictError("Session was moved or deleted; metadata was not recreated")
        data = _index(root)
        data["sessions"][spec["session_id"]] = {**data["sessions"].get(spec["session_id"], {}), **patch}
        if "title" in patch: data["sessions"][spec["session_id"]]["title"] = patch["title"].strip()
        atomic_json(root / "index.json", data)
    finally:
        lock.close()
    return {"completed": True, "session_id": spec["session_id"]}


def mutate(spec, held=()):
    if spec.get("action") == "workbench":
        from xuloom.desktop.settings import save_workbench
        return save_workbench(spec)
    if spec.get("action") == "recover":
        return recover_storage()
    if spec.get("action") == "migrate":
        from xuloom.core.storage import migrate_project
        return {"completed": True, "root": str(migrate_project(spec["project"]))}
    if spec.get("action") == "metadata":
        return _metadata(spec)
    if spec.get("action") not in {"create", "move", "fork", "delete"}:
        raise ValueError("Unsupported session storage operation")
    rows = spec.get("sessions", []) if spec["action"] == "delete" else [{"project": spec["project"], "session_id": spec["session_id"]}]
    if spec["action"] == "delete" and not rows:
        return {"completed": True, "session_id": None}
    if spec["action"] == "fork" and spec["new_id"] == spec["session_id"]:
        raise ValueError("Fork requires a new session identity")
    ids = {row["session_id"] for row in rows}
    projects = {row["project"] for row in rows}
    if spec["action"] in {"move", "fork"}: projects.add(spec["target"])
    if spec["action"] == "fork": ids.add(spec["new_id"])
    ids.update("index_" + history_root(project).name for project in projects)
    request_hash = _hash(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    with ExitStack() as stack:
        for identity in sorted(ids - set(held)):
            lock = SessionLock(data_directory() / "session-locks", identity)
            lock.acquire(); stack.callback(lock.close)
        recovered = [recover_pending(identity, set(held) | ids) for identity in sorted(ids)]
        if request_hash in recovered:
            return {"completed": True, "session_id": spec.get("new_id", spec.get("session_id")), "recovered": True}
        plan = _plan(spec, sorted(ids))
        plan["request_hash"] = request_hash
        token = uuid.uuid4().hex
        path = data_directory() / "session-operations" / ("transaction-" + token + ".json")
        plan["phase"] = "prepared"
        atomic_json(path, plan)
        for identity in sorted(ids): atomic_json(_pointer(identity), {"transaction": token})
        plan["phase"] = "committing"
        atomic_json(path, plan)
        _apply(plan)
        plan["phase"] = "completed"
        atomic_json(path, plan)
        _clean(plan, path)
    return {"completed": True, "session_id": spec.get("new_id", spec.get("session_id"))}


if __name__ == "__main__":
    import sys
    # The isolated bundled interpreter ignores PYTHONIOENCODING on Windows.
    # This JSON protocol is UTF-8 in both directions, independent of the OS code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(mutate(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
