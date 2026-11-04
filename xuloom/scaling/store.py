"""Small transactional index; conversation bodies remain in native session history."""
import copy
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from xuloom.core.storage import data_directory

IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("Invalid Scaling identity")
    return value


def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex


class Store:
    def __init__(self, path=None):
        self.path = Path(path or data_directory() / "scaling" / "index.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            try:
                db.execute("SELECT json('{}')").fetchone()
                self.json_projection = True
            except sqlite3.OperationalError:
                self.json_projection = False
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, parent TEXT NOT NULL,
                    status TEXT NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS record_parent ON records(kind,parent,status,updated);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, parent TEXT NOT NULL,
                    id TEXT NOT NULL UNIQUE, at REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_parent ON events(parent,seq);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def put_db(db, kind, value):
        value = copy.deepcopy(value)
        identity(value["id"])
        value["updated"] = time.time()
        db.execute("INSERT INTO records VALUES(?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET "
                   "parent=excluded.parent,status=excluded.status,updated=excluded.updated,data=excluded.data",
                   (kind, value["id"], value.get("run_id", ""), value.get("status", ""), value["updated"],
                    json.dumps(value, ensure_ascii=False, allow_nan=False)))
        return value

    def put(self, kind, value):
        with self.connection() as db:
            return self.put_db(db, kind, value)

    def get(self, kind, key):
        identity(key)
        with self.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row:
            raise ValueError("Unknown Scaling " + kind + ": " + key)
        return json.loads(row[0])

    def update(self, kind, key, change):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, identity(key))).fetchone()
            if not row:
                raise ValueError("Unknown Scaling record")
            value = json.loads(row[0])
            result = change(value)
            return self.put_db(db, kind, value if result is None else result)

    @staticmethod
    def brief(value):
        value.pop("text", None)
        value.pop("report", None)
        if isinstance(value.get("receipt"), dict): value["receipt"].pop("text", None)
        return value

    def list(self, kind, parent=None, statuses=None, offset=0, limit=100, brief=False):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid Scaling page")
        data = "json_remove(data,'$.text','$.report','$.receipt.text')" if brief and self.json_projection else "data"
        sql, args = "SELECT " + data + " FROM records WHERE kind=?", [kind]
        if parent is not None:
            sql += " AND parent=?"; args.append(parent)
        if statuses is not None:
            if not statuses:
                return []
            sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"; args.extend(statuses)
        sql += " ORDER BY updated,id LIMIT ? OFFSET ?"; args.extend([limit, offset])
        with self.connection() as db:
            return [self.brief(json.loads(row[0])) if brief else json.loads(row[0]) for row in db.execute(sql, args)]

    def counts(self, kind, parent):
        with self.connection() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM records WHERE kind=? AND parent=? GROUP BY status", (kind, parent)))

    def walk(self, kind, parent=None, statuses=None, brief=False):
        """Keyset pages are stable while status/progress timestamps change."""
        cursor = ""
        while True:
            data = "json_remove(data,'$.text','$.report','$.receipt.text')" if brief and self.json_projection else "data"
            sql, args = "SELECT id," + data + " FROM records WHERE kind=? AND id>?", [kind, cursor]
            if parent is not None:
                sql += " AND parent=?"; args.append(parent)
            if statuses is not None:
                if not statuses: return
                sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"; args.extend(statuses)
            sql += " ORDER BY id LIMIT 200"
            found = False
            with self.connection() as db:
                for key, data in db.execute(sql, args):
                    found, cursor = True, key
                    value = json.loads(data)
                    data = None
                    yield self.brief(value) if brief else value
            if not found: return

    def claim(self, key, owner):
        def change(row):
            if row["status"] != "queued":
                raise ValueError("Task is already claimed or not ready")
            row.update(status="dispatching", owner=identity(owner))
        return self.update("task", key, change)

    def event(self, parent, value, event_id=None):
        with self.connection() as db:
            self.event_db(db, parent, value, event_id)

    @staticmethod
    def event_db(db, parent, value, event_id=None):
        db.execute("INSERT OR IGNORE INTO events(parent,id,at,data) VALUES(?,?,?,?)",
                   (parent, event_id or new_id("event"), time.time(), json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def events(self, parent, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor")
        with self.connection() as db:
            return [{"cursor": row[0], "at": row[1], **json.loads(row[2])} for row in db.execute(
                "SELECT seq,at,data FROM events WHERE parent=? AND seq>? ORDER BY seq LIMIT ?", (parent, after, limit))]

    def cursor(self, parent):
        with self.connection() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE parent=?", (parent,)).fetchone()[0]
