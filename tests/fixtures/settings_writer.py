"""Disposable desktop-settings writers for concurrency and crash acceptance."""
import json
import os
from pathlib import Path
import sys
from xuloom.core.session_mutation import mutate

if sys.argv[2] == "hold":
    from xuloom.core.session_lock import SessionLock
    from xuloom.core.storage import data_directory
    lock = SessionLock(data_directory() / "session-locks", "preferences_workbench"); lock.acquire()
    print("OWNED", flush=True)
    try: sys.stdin.readline()
    finally: lock.close()
    raise SystemExit(0)

specs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if sys.argv[2] == "race":
    print("READY", flush=True)
    sys.stdin.readline()
elif sys.argv[2] == "crash":
    from xuloom.desktop import settings
    write = settings.atomic_json
    def crash(path, value, **options):
        write(path, value, **options)
        os._exit(87)
    settings.atomic_json = crash
for spec in specs:
    assert mutate({"action": "workbench", **spec})["completed"]
print("SAVED", flush=True)
