"""Per-connection request admission using existing atomic files and OS leases.

No daemon, network proxy, model retries or second agent scheduler. A stream owns
its slot until closed; crashed processes release their permits through the OS.
"""
import asyncio
import copy
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import time
import uuid

from xuloom.core.session_lock import SessionBusyError, SessionLock
from xuloom.core.storage import IOPath, atomic_json, data_directory


def limits(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or value.keys() - {"concurrency", "requests_per_minute"}:
        raise ValueError("Invalid model request limits")
    result = {"concurrency": value.get("concurrency", 4), "requests_per_minute": value.get("requests_per_minute")}
    if type(result["concurrency"]) is not int or not 1 <= result["concurrency"] <= 1024:
        raise ValueError("Model request concurrency must be 1–1024")
    rate = result["requests_per_minute"]
    if rate is not None and (type(rate) not in {int, float} or not math.isfinite(rate) or rate <= 0):
        raise ValueError("Model requests_per_minute must be positive")
    return result


def connection_id(url, key):
    return hashlib.sha256(json.dumps([str(url).rstrip("/"), key], ensure_ascii=False).encode()).hexdigest()


def retry_delay(headers, now=None):
    now = time.time() if now is None else now
    for name, factor in (("retry-after", 1), ("retry-after-ms", .001)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * factor
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - now
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds):
            return max(0, min(seconds, 86400))
    return 1.0


class RequestFlow:
    def __init__(self, identity, policy=None, directory=None):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Invalid connection identity")
        self.root = IOPath(directory or data_directory() / "request-limits") / identity
        self._settings = policy if callable(policy) else None
        self.policy = limits(policy() if self._settings else policy)
        self._checked_at = time.monotonic()

    def current_policy(self, force=False):
        if self._settings and (force or time.monotonic() - self._checked_at >= .25):
            self.policy = limits(self._settings())
            self._checked_at = time.monotonic()
        return limits(self.policy)

    @staticmethod
    def validate_state(value):
        def valid_hash(item):
            return isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
        if not isinstance(value, dict) or not all(isinstance(value.get(k), list) for k in ("queue", "active", "turns")):
            raise ValueError("Model request queue is damaged; existing records were preserved")
        try:
            if not {"cooldown_until", "next_request_at"} <= value.keys():
                raise ValueError("missing admission timestamps")
            for key in ("cooldown_until", "next_request_at", "last_started_at", "pruned_at"):
                stamp = value.get(key, 0)
                if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0:
                    raise ValueError("invalid timestamp")
            identities = set()
            for row in value["queue"] + value["active"]:
                if (not isinstance(row, dict) or row.keys() - {"id", "owner", "limits"} or
                        not isinstance(row.get("id"), str) or not re.fullmatch(r"request-[a-f0-9]{32}", row["id"]) or
                        row["id"] in identities or not valid_hash(row.get("owner"))):
                    raise ValueError("invalid request identity")
                identities.add(row["id"])
                if "limits" in row:
                    if not isinstance(row["limits"], dict):
                        raise ValueError("invalid saved request limits")
                    limits(row["limits"])
            turns = value["turns"]
            if not all(valid_hash(owner) for owner in turns) or len(set(turns)) != len(turns) or set(turns) != {row["owner"] for row in value["queue"]}:
                raise ValueError("invalid fairness order")
            if value.get("last_owner") is not None and not valid_hash(value["last_owner"]):
                raise ValueError("invalid previous owner")
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Model request queue is damaged; existing records were preserved") from None

    @asynccontextmanager
    async def state(self):
        lock = SessionLock(self.root, "state")
        while True:
            try:
                lock.acquire()
                break
            except SessionBusyError:
                await asyncio.sleep(.05)
        try:
            path = self.root / "state.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                value = {"queue": [], "active": [], "turns": [], "cooldown_until": 0, "next_request_at": 0}
            # A damaged journal must not create a second independent capacity.
            self.validate_state(value)
            before = copy.deepcopy(value)
            yield value
            if value != before or not path.exists():
                atomic_json(path, value)
        finally:
            lock.close()

    def prune(self, state):
        elapsed = time.time() - state.get("pruned_at", 0)
        if not 0 <= elapsed < .5:
            for key in ("queue", "active"):
                state[key] = [row for row in state[key] if SessionLock(self.root, row["id"]).locked() is not False]
            state["pruned_at"] = time.time()
        owners = {row["owner"] for row in state["queue"]}
        state["turns"] = [owner for owner in state["turns"] if owner in owners]

    @staticmethod
    def admission_limits(state, policy):
        policies = [policy] + [limits(r["limits"]) for r in state["active"] + state["queue"] if "limits" in r]
        capacity = min(p["concurrency"] for p in policies)
        rates = [p["requests_per_minute"] for p in policies if p["requests_per_minute"] is not None]
        rate = min(rates) if rates else None
        quota_at = (state["last_started_at"] + 60 / rate if rate else 0) if "last_started_at" in state else state["next_request_at"]
        return capacity, rate, quota_at

    async def acquire(self, owner):
        policy = self.current_policy(force=True)
        owner = hashlib.sha256(str(owner).encode()).hexdigest()
        lease = SessionLock(self.root, "request-" + uuid.uuid4().hex)
        lease.acquire()
        permit = Permit(self, lease)
        row = {"id": lease.identity, "owner": owner, "limits": policy}
        try:
            async with self.state() as state:
                self.prune(state)
                state["queue"].append(row)
                if owner not in state["turns"]:
                    state["turns"].append(owner)
            while True:
                policy = self.current_policy()
                async with self.state() as state:
                    self.prune(state)
                    for queued in state["queue"]:
                        if queued["id"] == row["id"]:
                            queued["limits"] = policy
                    capacity, rate, quota_at = self.admission_limits(state, policy)
                    if len(state["turns"]) > 1 and state["turns"][0] == state.get("last_owner"):
                        state["turns"].append(state["turns"].pop(0))
                    head = next((r for r in state["queue"] if r["owner"] == state["turns"][0]), None) if state["turns"] else None
                    if (head and head["id"] == row["id"] and len(state["active"]) < capacity and
                            time.time() >= max(state["cooldown_until"], quota_at) and self._settings):
                        # Recheck the authoritative setting at admission, not
                        # just when this waiter last refreshed its cache.
                        policy = self.current_policy(force=True)
                        head["limits"] = policy
                        capacity, rate, quota_at = self.admission_limits(state, policy)
                    if (head and head["id"] == row["id"] and len(state["active"]) < capacity and
                            time.time() >= max(state["cooldown_until"], quota_at)):
                        state["queue"].remove(head)
                        state["active"].append(head)
                        state["last_owner"] = owner
                        state["turns"].pop(0)
                        if any(r["owner"] == owner for r in state["queue"]):
                            state["turns"].append(owner)
                        state["last_started_at"] = time.time()
                        state["next_request_at"] = state["last_started_at"] + 60 / rate if rate else 0
                        return permit
                await asyncio.sleep(.05)
        except BaseException:
            await asyncio.shield(permit.release())
            raise

    async def cooldown(self, seconds):
        if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Invalid connection cooldown")
        async with self.state() as state:
            state["cooldown_until"] = max(state["cooldown_until"], time.time() + seconds)


class Permit:
    def __init__(self, flow, lease):
        self.flow, self.lease = flow, lease

    async def release(self):
        lease, self.lease = self.lease, None
        if lease is None:
            return
        try:
            async with self.flow.state() as state:
                for key in ("queue", "active"):
                    state[key] = [row for row in state[key] if row["id"] != lease.identity]
                self.flow.prune(state)
        finally:
            lease.close()
            try:
                lease.path.unlink()
            except FileNotFoundError:
                pass


def http_client_type(base, flow, owner):
    """Wrap each SDK's own compatible HTTP implementation, including httpx2."""
    import importlib
    library = importlib.import_module(next(cls.__module__.split('.')[0] for cls in base.__mro__ if cls.__module__.split('.')[0] in {"httpx", "httpx2"}))

    class Stream(library.AsyncByteStream):
        def __init__(self, stream, permit):
            self.stream, self.permit = stream, permit
            self.closed = False

        async def __aiter__(self):
            try:
                async for chunk in self.stream:
                    yield chunk
            finally:
                await self.aclose()

        async def aclose(self):
            if self.closed:
                return
            self.closed = True
            try:
                await self.stream.aclose()
            finally:
                await asyncio.shield(self.permit.release())

    class Client(base):
        async def send(self, *args, **kwargs):
            permit = await flow.acquire(owner() if callable(owner) else owner)
            try:
                response = await super().send(*args, **kwargs)
                if response.status_code == 429 or response.status_code == 503 and "retry-after" in response.headers:
                    await flow.cooldown(retry_delay(response.headers))
                if response.is_closed:
                    await permit.release()
                else:
                    response.stream = Stream(response.stream, permit)
                return response
            except BaseException:
                await asyncio.shield(permit.release())
                raise

    return Client
