"""Shared per-user history storage; working directories contain configuration and user files."""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


class IOPath(type(Path())):
    """Keep logical paths ordinary while Windows filesystem calls use long paths."""
    def __fspath__(self):
        value = str(self)
        if os.name == "nt" and self.is_absolute() and not value.startswith("\\\\?\\"):
            return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
        return value


def logical_path(path):
    value = str(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"): value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"): value = value[4:]
    return Path(value)


def data_directory():
    override = os.environ.get("XULOOM_DATA_DIR")
    if override and Path(override).is_absolute():
        return Path(override)
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base and Path(base).is_absolute() else home / "AppData/Local") / "Xuloom"
    if sys.platform == "darwin":
        return home / "Library/Application Support/Xuloom"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base and Path(base).is_absolute() else home / ".local/share") / "xuloom"


def history_root(cwd=None):
    project = os.path.realpath(cwd or os.getcwd())
    identity = project.lower() if os.name == "nt" else project
    return data_directory() / "history" / hashlib.sha256(identity.encode()).hexdigest()[:16]


def atomic_json(path, payload, *, ensure_ascii=False):
    atomic_text(path, json.dumps(payload, ensure_ascii=ensure_ascii, indent=2))


def atomic_text(path, text, *, preserve_mode=False, create_only=False):
    content = text.encode("utf-8")
    _atomic_write(path, lambda stream: stream.write(content), preserve_mode=preserve_mode, create_only=create_only)


def atomic_bytes(path, content):
    _atomic_write(path, lambda stream: stream.write(content))


def file_hash(path):
    digest = hashlib.sha256()
    with IOPath(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(path, source, expected_hash, *, preserve_mode=False):
    """Publish a streamed copy only after verifying the complete source bytes."""
    def write(stream):
        digest = hashlib.sha256()
        with IOPath(source).open("rb") as original:
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                stream.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            from xuloom.core.session_lock import SessionConflictError
            raise SessionConflictError("Source changed during a session operation; records were preserved")
    _atomic_write(path, write, preserve_mode=preserve_mode)


def _atomic_write(path, write, *, preserve_mode=False, create_only=False):
    path = IOPath(path); path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if preserve_mode and path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".migration-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if create_only:
            # Linking a fully flushed sibling temp publishes without replacing a competing file.
            os.link(temporary, path)
            return
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                # Windows readers/scanners can briefly hold a destination without delete sharing.
                if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        if os.path.exists(temporary):
            if preserve_mode and os.name == "nt":
                os.chmod(temporary, 0o600)
            os.unlink(temporary)


def rebase(value, source, target):
    if isinstance(value, str):
        converted = value.replace(str(source), str(target))
        return converted.replace(str(source).replace("\\", "/"), str(target).replace("\\", "/")) if "\\" in str(source) else converted
    if isinstance(value, list): return [rebase(item, source, target) for item in value]
    if isinstance(value, dict): return {name: rebase(item, source, target) for name, item in value.items()}
    return value


def migrate_project(cwd=None):
    """Upgrade legacy storage under the same OS leases as current history writers."""
    from contextlib import ExitStack
    from xuloom.core.session_lock import SessionLock
    project = Path(os.path.realpath(cwd or os.getcwd()))
    legacy, target = project / ".xuloom", history_root(project)
    kinds = ("sessions", "session-index.json", "tool-results", "browser", "shell-tasks", "vcr", "notebooks")
    with ExitStack() as stack:
        if any((legacy / kind).exists() for kind in kinds):
            # A project-wide migration can touch legacy resources shared by several sessions.
            # Preflight all current and incoming owners before copying or removing anything.
            lock = SessionLock(data_directory() / "session-locks", "index_" + target.name)
            lock.acquire(); stack.callback(lock.close)
            identities = {file.stem for root in (legacy / "sessions", target / "sessions")
                if root.is_dir() for file in root.glob("*.json")}
            for identity in sorted(identities):
                lock = SessionLock(data_directory() / "session-locks", identity)
                lock.acquire(); stack.callback(lock.close)
            from xuloom.core.session_mutation import _safe, recover_pending
            _safe(target / "sessions")
            for identity in sorted(identities):
                recover_pending(identity, identities | {"index_" + target.name})
        return _migrate_project(cwd)


def _migrate_project(cwd=None):
    project = Path(os.path.realpath(cwd or os.getcwd())); target = history_root(project); legacy = project / ".xuloom"
    journal_path = target / "migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
    artifacts = legacy / "tool-results"
    runtime_kinds = ["browser", "shell-tasks", "vcr", "notebooks"]
    def transfer(source, destination, transform=False):
        if not source.exists(): return
        if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
            raise ValueError("不能迁移链接形式的会话资源")
        if source.is_dir():
            for child in source.iterdir(): transfer(child, destination / child.name, transform)
            try: source.rmdir()
            except OSError: pass
            return
        original = source.read_bytes(); digest = hashlib.sha256(original).hexdigest(); content = original; stat = source.stat()
        if transform and source.suffix == ".json":
            try:
                value = json.loads(original)
                for name in ["tool-results", *runtime_kinds]:
                    value = rebase(value, legacy / name, target / name)
                    value = rebase(value, Path(".xuloom") / name, target / name)
                content = json.dumps(value, ensure_ascii=False, indent=2).encode()
            except (ValueError, UnicodeError): pass
        # A journal entry is not proof the destination still exists or is unchanged.
        if destination.exists():
            try: same = json.loads(destination.read_bytes()) == json.loads(content) if transform else destination.read_bytes() == content
            except (ValueError, UnicodeError): same = destination.read_bytes() == content
            if not same: raise ValueError("旧会话与集中存储冲突，双方记录已保留：" + str(source))
        else:
            atomic_bytes(destination, content)
            os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        journal[str(source)] = digest; atomic_json(journal_path, journal)
        if source.read_bytes() != original: raise ValueError("迁移过程中旧会话被其他程序修改，原文件保留")
        try: source.unlink()
        except PermissionError: pass
    if any((legacy / name).exists() for name in ["sessions", "session-index.json", "tool-results", *runtime_kinds]):
        if legacy.is_symlink() or (hasattr(legacy, "is_junction") and legacy.is_junction()): raise ValueError("不能迁移链接形式的历史目录")
        transfer(artifacts, target / "tool-results")
        for name in runtime_kinds: transfer(legacy / name, target / name)
        transfer(legacy / "sessions", target / "sessions", True)
        old, current = legacy / "session-index.json", target / "index.json"
        if old.exists() and current.exists():
            previous = json.loads(old.read_text(encoding="utf-8")); present = json.loads(current.read_text(encoding="utf-8"))
            if previous.get("version") != 1 or present.get("version") != 1: raise ValueError("旧会话索引格式不正确")
            atomic_json(current, {"version": 1, "sessions": {**previous.get("sessions", {}), **present.get("sessions", {})}})
            try: old.unlink()
            except PermissionError: pass
        else: transfer(old, current)
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / "project.json"
    if not manifest.exists(): atomic_json(manifest, {"version": 1, "project": str(project)})
    return target


def session_directory(cwd=None):
    return migrate_project(cwd) / "sessions"


def artifact_directory(cwd=None):
    return history_root(cwd) / "tool-results"


def migrate_user_file(old, new):
    old, new = Path(old), Path(new)
    if old.is_file() and not old.is_symlink():
        if not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            with new.open("xb") as stream: stream.write(old.read_bytes())
        if new.read_bytes() == old.read_bytes():
            try: old.unlink()
            except PermissionError: pass
    return new
