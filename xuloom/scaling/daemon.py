"""Owned loopback RPC services, reached remotely only through existing SSH stdio.

The endpoint secret stays on its host. An SSH disconnect does not kill accepted
work. No listening socket is exposed to the LAN, no shell commands are built.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from xuloom.core.session_lock import SessionLock, SessionBusyError
from xuloom.core.storage import data_directory, atomic_json
from .store import transient

MAX_FRAME = 64 * 1024 * 1024
IDLE_SECONDS = 120
AUTH_FAILED = "Scaling service authentication failed"
SERVICE_STOPPING = "Scaling service is stopping before accepting this request"


class StaleEndpoint(ConnectionError):
    """The endpoint stopped or rejected authentication before accepting work."""


def endpoint(name):
    if name not in {"node", "control"}:
        raise ValueError("Invalid Scaling service")
    return data_directory() / "scaling" / (name + "-endpoint.json")


async def call(address, method, params=None, timeout=60):
    reader, writer = await asyncio.open_connection("127.0.0.1", address["port"], limit=MAX_FRAME)
    try:
        payload = {"secret": address["secret"], "method": method, "params": params or {}}
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ConnectionError("Scaling service disconnected; outcome is unconfirmed")
        response = json.loads(line)
        if "error" in response:
            if response["error"] in {AUTH_FAILED, SERVICE_STOPPING}:
                raise StaleEndpoint(response["error"])
            raise ValueError(response["error"])
        return response["result"]
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def connect(name):
    path = endpoint(name)
    async def probe():
        try:
            address = json.loads(path.read_text(encoding="utf-8"))
            await call(address, "ping", timeout=2)
            return address
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            return None
    address = await probe()
    if address:
        return address
    guard = SessionLock(path.parent / "locks", name + "-launch")
    for _ in range(100):
        try:
            guard.acquire()
            break
        except SessionBusyError:
            await asyncio.sleep(.1)
    else:
        raise ValueError("Scaling service startup is already in progress")
    try:
        address = await probe()
        if address:
            return address
        path.parent.mkdir(parents=True, exist_ok=True)
        log = path.parent / (name + "-service.log")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        # -I is used for packaged installs; source development needs an
        # explicit, application-owned import root, never a shell string.
        bootstrap = "import sys,runpy;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[2])) + ");runpy.run_module('xuloom.scaling." + name + "',run_name='__main__')"
        def launch():
            with log.open("ab") as output:
                return subprocess.Popen([sys.executable, "-I", "-u", "-c", bootstrap], stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=output, **options)
        process = launch()
        relaunched = False
        for _ in range(150):
            address = await probe()
            if address:
                return address
            if not relaunched and process.poll() is not None:
                # The old endpoint can release its service lease just after
                # this process starts and loses the lease race.
                await asyncio.sleep(.5)
                process = launch()
                relaunched = True
            await asyncio.sleep(.1)
        raise ValueError("Scaling service did not start; inspect its local service log")
    finally:
        guard.close()


async def serve(name, service):
    path = endpoint(name)
    lease = SessionLock(path.parent / "locks", name + "-service")
    try:
        lease.acquire()
    except SessionBusyError:
        return
    secret = secrets.token_hex(32)
    stopped = asyncio.Event()
    last_request = time.monotonic()
    in_flight = 0
    async def handle(reader, writer):
        nonlocal last_request, in_flight
        in_flight += 1
        try:
            raw = await asyncio.wait_for(reader.readline(), 15)
            message = json.loads(raw)
            if not secrets.compare_digest(str(message.get("secret", "")), secret):
                raise ValueError(AUTH_FAILED)
            if stopped.is_set():
                raise ValueError(SERVICE_STOPPING)
            method = message.get("method")
            if method == "ping": result = {"protocol": 1}
            elif method == "service.stop":
                if in_flight > 1 or not service.can_stop(): raise ValueError("Scaling still owns unfinished work; pause/cancel and reconcile it first")
                result = True
                stopped.set()
            else: result = await service.dispatch(method, message.get("params") or {})
            response = {"result": result}
        except Exception as error:
            response = {"error": str(error)}
        try:
            writer.write((json.dumps(response, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            await writer.drain()
        except (OSError, ConnectionError):
            pass  # Accepted work owns its durable checkpoint, not this socket.
        finally:
            in_flight -= 1
            last_request = time.monotonic()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
    async def idle():
        # An idle service exits so installed-app updates are not held open by
        # it; the next Scaling call starts it again.
        while not stopped.is_set():
            await asyncio.sleep(min(5, idle_seconds))
            if in_flight or time.monotonic() - last_request < idle_seconds:
                continue
            try:
                if service.can_stop():
                    stopped.set()
            except Exception as error:
                if not transient(error):
                    raise
    try:
        idle_seconds = float(os.environ.get("XULOOM_SCALING_IDLE_SECONDS") or IDLE_SECONDS)
    except ValueError:
        idle_seconds = IDLE_SECONDS
    if not math.isfinite(idle_seconds) or idle_seconds <= 0:
        idle_seconds = IDLE_SECONDS
    server = None
    watcher = None
    try:
        await service.start()
        server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=MAX_FRAME)
        atomic_json(path, {"port": server.sockets[0].getsockname()[1], "secret": secret, "pid": os.getpid()})
        if os.name != "nt":
            path.chmod(0o600)
        watcher = asyncio.create_task(idle())
        async with server:
            await stopped.wait()
    finally:
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if server:
            server.close()
            await server.wait_closed()
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid(): path.unlink()
        except (OSError, ValueError): pass
        try:
            await service.close()
        finally:
            lease.close()


async def request(name, method, params=None, *, timeout=60, address=None):
    """Reconnect only if the original request was rejected before dispatch."""
    address = address or await connect(name)
    try:
        return await call(address, method, params, timeout=timeout), address
    except (ConnectionRefusedError, StaleEndpoint):
        address = await connect(name)
        return await call(address, method, params, timeout=timeout), address


class Gateway:
    """Stdio endpoint for the existing OpenSSH client; model-free and stateless."""
    def __init__(self, emit):
        self.address = None

    async def dispatch(self, method, params):
        if method == "runtime.info":
            from xuloom import __version__
            return {"protocol": 1, "scaling": 1, "version": __version__}
        if method == "shutdown":
            return True
        if not method.startswith("scaling.node."):
            raise ValueError("This endpoint only exposes owned Scaling executions")
        try:
            result, self.address = await request("node", method.removeprefix("scaling.node."), params, address=self.address)
            return result
        except (OSError, asyncio.TimeoutError):
            self.address = None
            raise

    async def close(self):
        pass  # Only the SSH gateway exits; accepted node executions persist.


def gateway_main():
    from xuloom.desktop.bridge import serve as stdio
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    asyncio.run(stdio(Gateway))
