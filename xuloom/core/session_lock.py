"""An OS-owned writer lease. The marker remains; closing the handle releases ownership."""
import json
import os
from xuloom.core.storage import IOPath
import re
import time


class SessionBusyError(OSError):
    pass


class SessionConflictError(OSError):
    pass


class SessionLock:
    def __init__(self, directory, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
            raise ValueError("Invalid session identity")
        self.path = IOPath(directory) / (identity + ".lock")
        self.identity = identity
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise SessionBusyError("Session is in use by another writer. Viewing is allowed; close or release the existing writer before continuing.") from None
        self.handle = handle
        try:
            # Append mode is needed only for safe creation; write metadata through a separate descriptor
            # while the locked byte and its inode are retained.
            with self.path.open("r+b") as metadata:
                metadata.seek(1)
                metadata.write(json.dumps({"pid": os.getpid(), "session_id": self.identity, "acquired_at": time.time()}).encode())
                metadata.truncate()
        except BaseException:
            self.close()
            raise

    def close(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __del__(self):
        if self.handle is not None:
            self.close()
