"""A small HTTP + Server-Sent Events API over QueryEngine (no extra dependencies).

Every request except ``GET /v1/health`` needs ``Authorization: Bearer <token>``.
Workspaces must lie inside ``root``. See ``docs/server.md`` for the endpoints.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

__all__ = ["XuloomServer", "HttpError"]

_logger = logging.getLogger("xuloom.server")
_MAX_HEADER = 64 * 1024
_MAX_BODY = 10 * 1024 * 1024
_DECISIONS = {"allow", "always", "deny", "never"}
_STATUS = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
           404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 411: "Length Required",
           413: "Payload Too Large", 500: "Internal Server Error", 501: "Not Implemented"}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class _Request:
    method: str
    path: str
    headers: Dict[str, str]
    body: bytes

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpError(400, f"Body is not valid JSON: {error}") from None
        if not isinstance(value, dict):
            raise HttpError(400, "Body must be a JSON object")
        return value


@dataclass
class _Session:
    id: str
    engine: Any
    cwd: Path
    events: "asyncio.Queue[dict]" = field(default_factory=asyncio.Queue)
    run: Optional[asyncio.Task] = None
    pending: Dict[str, dict] = field(default_factory=dict)
    futures: Dict[str, asyncio.Future] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.run is not None and not self.run.done()


class XuloomServer:
    """Serve sessions of ``QueryEngine(config.replace(cwd=...))`` over HTTP.

    ``config`` supplies the model connection and defaults; ``root`` bounds every
    workspace; ``token`` is generated when omitted (read it from ``server.token``).
    """

    def __init__(self, config=None, *, root=".", token: Optional[str] = None, host: str = "127.0.0.1", port: int = 8765,
                 approval_timeout: float = 600.0):
        from xuloom.core.engine_config import EngineConfig
        self.config = config or EngineConfig()
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Server root must be a directory")
        self.token = token or secrets.token_urlsafe(32)
        if len(self.token) < 16:
            raise ValueError("The server token must be at least 16 characters")
        self.host, self.port = host, int(port)
        self.approval_timeout = float(approval_timeout)
        self.sessions: Dict[str, _Session] = {}
        self._server: Optional[asyncio.base_events.Server] = None
        self._connections: set = set()

    @property
    def is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return self.host == "localhost"

    async def start(self) -> Tuple[str, int]:
        self._server = await asyncio.start_server(self._connection, self.host, self.port, limit=_MAX_HEADER)
        address = self._server.sockets[0].getsockname()
        self.port = address[1]
        if not self.is_loopback:
            _logger.warning("Xuloom server is listening on %s:%s; anyone with the token can run tools in %s",
                            self.host, self.port, self.root)
        return self.host, self.port

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for session in list(self.sessions.values()):
            await self._close_session(session)
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)

    # ---- HTTP plumbing -------------------------------------------------------
    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            try:
                request = await self._read_request(reader)
                await self._route(request, writer, reader)
            except HttpError as error:
                await self._send_json(writer, error.status, {"error": str(error)})
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            except Exception as error:
                _logger.exception("Server request failed")
                await self._send_json(writer, 500, {"error": f"{type(error).__name__}: {error}"})
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._connections.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> _Request:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            raise HttpError(413, "Request headers are too large") from None
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ")
        except ValueError:
            raise HttpError(400, "Malformed request line") from None
        if not version.startswith("HTTP/1."):
            raise HttpError(400, "Only HTTP/1.x is supported")
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise HttpError(400, "Malformed header")
            headers[name.strip().lower()] = value.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise HttpError(501, "Chunked request bodies are not supported; send Content-Length")
        try:
            length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            raise HttpError(400, "Invalid Content-Length") from None
        if length < 0 or length > _MAX_BODY:
            raise HttpError(413, "Request body is too large")
        body = await reader.readexactly(length) if length else b""
        return _Request(method.upper(), unquote(urlsplit(target).path), headers, body)

    async def _send_json(self, writer, status: int, payload: Any) -> None:
        body = b"" if status == 204 else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        writer.write((f"HTTP/1.1 {status} {_STATUS.get(status, 'Error')}\r\n"
                      "Content-Type: application/json; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n"
                      "Cache-Control: no-store\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

    def _authorize(self, request: _Request) -> None:
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + self.token
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise HttpError(401, "Missing or invalid bearer token")

    # ---- routes ----------------------------------------------------------------
    async def _route(self, request: _Request, writer, reader=None) -> None:
        parts = [part for part in request.path.split("/") if part]
        if parts[:1] != ["v1"]:
            raise HttpError(404, "Unknown path")
        parts = parts[1:]
        if parts == ["health"] and request.method == "GET":
            return await self._send_json(writer, 200, {"ok": True})
        self._authorize(request)
        if parts == ["sessions"]:
            if request.method == "GET":
                return await self._send_json(writer, 200, {"sessions": [self._describe(s) for s in self.sessions.values()]})
            if request.method == "POST":
                return await self._send_json(writer, 201, await self._create_session(request.json()))
            raise HttpError(405, "Use GET or POST")
        if len(parts) < 2 or parts[0] != "sessions":
            raise HttpError(404, "Unknown path")
        session = self.sessions.get(parts[1])
        if session is None:
            raise HttpError(404, "Unknown session")
        action = parts[2:]
        if not action:
            if request.method == "GET":
                return await self._send_json(writer, 200, self._describe(session))
            if request.method == "DELETE":
                await self._close_session(session)
                return await self._send_json(writer, 204, None)
            raise HttpError(405, "Use GET or DELETE")
        if request.method != "POST":
            raise HttpError(405, "Use POST")
        if action == ["messages"]:
            return await self._message(session, request, writer, reader)
        if action == ["abort"]:
            await session.engine.request_abort()
            return await self._send_json(writer, 202, {"aborting": session.running})
        if len(action) == 2 and action[0] in {"approvals", "answers"}:
            return await self._send_json(writer, 200, self._resolve(session, action[0], action[1], request.json()))
        raise HttpError(404, "Unknown path")

    def _workspace(self, relative: Any) -> Path:
        if relative in (None, ""):
            return self.root
        if not isinstance(relative, str):
            raise HttpError(400, "'cwd' must be a path relative to the server root")
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise HttpError(400, "'cwd' must stay inside the server root")
        if not target.is_dir():
            raise HttpError(400, "'cwd' does not exist")
        return target

    async def _create_session(self, body: dict) -> dict:
        from xuloom.core.engine import QueryEngine
        from xuloom.security.permissions import ApprovalPolicy
        unknown = set(body) - {"cwd", "session_id", "permission", "collaboration_mode"}
        if unknown:
            raise HttpError(400, "Unknown field(s): " + ", ".join(sorted(unknown)))
        cwd = self._workspace(body.get("cwd"))
        changes: Dict[str, Any] = {"cwd": str(cwd)}
        if body.get("permission") is not None:
            if body["permission"] not in {"ask", "full"}:
                raise HttpError(400, "'permission' is 'ask' or 'full'")
            changes["approval_policy"] = ApprovalPolicy(body["permission"])
        if body.get("collaboration_mode") is not None:
            if body["collaboration_mode"] not in {"solo", "auto", "ultra"}:
                raise HttpError(400, "'collaboration_mode' is 'solo', 'auto' or 'ultra'")
            changes["collaboration_mode"] = body["collaboration_mode"]
        holder: Dict[str, _Session] = {}

        async def approve(permission_request):
            return await self._ask(holder["session"], "approval", {
                "tool_name": permission_request.tool_name, "summary": permission_request.summary,
                "risk_level": permission_request.risk_level, "input": permission_request.input_payload}, default="deny")

        engine = QueryEngine(self.config.replace(**changes), permission_handler=approve)
        try:
            if body.get("session_id"):
                if str(body["session_id"]) in self.sessions:
                    raise HttpError(409, "That session is already open on this server")
                if not engine.load_session(str(body["session_id"])):
                    raise HttpError(404, "Saved session not found in this workspace")
            if engine.session_id in self.sessions:
                raise HttpError(409, "That session is already open on this server")
            engine.require_writer()
        except BaseException:
            await engine.aclose()
            raise

        async def question(questions):
            answer = await self._ask(holder["session"], "question", {"questions": questions}, default=None)
            return answer if isinstance(answer, str) and answer.strip() else (
                "No answer was given. This task is blocked awaiting explicit user input; do not assume an answer.")
        engine.tool_context["ask_user_callback"] = question
        session = holder["session"] = _Session(engine.session_id, engine, cwd)
        self.sessions[session.id] = session
        return self._describe(session)

    async def _ask(self, session: _Session, kind: str, details: dict, *, default):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        session.futures[request_id] = future
        session.pending[request_id] = {"request_id": request_id, "kind": kind, **details}
        await session.events.put({"type": "approval_required" if kind == "approval" else "input_required",
                                  "request_id": request_id, **details})
        try:
            return await asyncio.wait_for(future, self.approval_timeout)
        except asyncio.TimeoutError:
            await session.events.put({"type": "request_expired", "request_id": request_id})
            return default
        finally:
            session.futures.pop(request_id, None)
            session.pending.pop(request_id, None)

    def _resolve(self, session: _Session, kind: str, request_id: str, body: dict) -> dict:
        pending = session.pending.get(request_id)
        future = session.futures.get(request_id)
        if pending is None or future is None or future.done():
            raise HttpError(404, "No pending request with that id")
        if kind == "approvals":
            if pending["kind"] != "approval":
                raise HttpError(400, "That request is a question; POST to /answers")
            decision = str(body.get("decision", "")).lower()
            if decision not in _DECISIONS:
                raise HttpError(400, "'decision' is one of " + ", ".join(sorted(_DECISIONS)))
            future.set_result(decision)
        else:
            if pending["kind"] != "question":
                raise HttpError(400, "That request is an approval; POST to /approvals")
            if not isinstance(body.get("answer"), str):
                raise HttpError(400, "'answer' must be a string")
            future.set_result(body["answer"])
        return {"resolved": request_id}

    async def _next_event(self, session: _Session, reader) -> Optional[dict]:
        """The next event, or ``None`` once the client closed its connection (checked while waiting)."""
        if reader is None:
            return await session.events.get()
        getter = asyncio.ensure_future(session.events.get())
        closed = asyncio.ensure_future(reader.read(1))
        try:
            done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (getter, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(getter, closed, return_exceptions=True)
        if getter in done and not getter.cancelled():
            return getter.result()
        # A request is fully read before the run starts, so any read completing here means
        # EOF/reset: the client is gone. (Extra bytes from a misbehaving client count as gone too.)
        return None

    async def _abandon(self, session: _Session) -> None:
        """Stop the run of a client that went away, even one that has not started yet.

        ``request_abort`` alone is not enough: a run that has been created but not begun
        clears the abort flag when it starts. Cancelling the task goes through the normal
        consumer-cancellation path of ``submit_message`` (the cancellation is persisted).
        """
        if session.running:
            assert session.run is not None
            await session.engine.request_abort()
            session.run.cancel()
            await asyncio.gather(session.run, return_exceptions=True)

    async def _message(self, session: _Session, request: _Request, writer, reader=None) -> None:
        body = request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "'text' is required")
        if session.running:
            raise HttpError(409, "A task is already running in this session")
        stream = bool(body.get("stream")) or "text/event-stream" in request.headers.get("accept", "")
        while not session.events.empty():
            session.events.get_nowait()
        session.run = asyncio.create_task(self._drive(session, text))
        if not stream:
            final = None
            while final is None:
                event = await self._next_event(session, reader)
                if event is None:
                    return await self._abandon(session)
                if event.get("type") == "final":
                    final = event
            return await self._send_json(writer, 200, {"text": final.get("text"), "result": final.get("result")})
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nConnection: close\r\n\r\n")
            await writer.drain()
            while True:
                event = await self._next_event(session, reader)
                if event is None:
                    return await self._abandon(session)
                data = json.dumps(event, ensure_ascii=False, default=str)
                writer.write(f"event: {event.get('type', 'message')}\ndata: {data}\n\n".encode("utf-8"))
                await writer.drain()
                if event.get("type") == "final":
                    break
        except (OSError, asyncio.CancelledError):
            # The client went away (even while the response head was being sent):
            # stop the task like any other cancelled consumer.
            await self._abandon(session)
            raise

    async def _drive(self, session: _Session, text: str) -> None:
        try:
            async for event in session.engine.submit_message(text, sys_print_callback=lambda *_: None):
                await session.events.put(event)
        except Exception as error:
            _logger.exception("Session %s failed", session.id)
            await session.events.put({"type": "final", "text": f"{type(error).__name__}: {error}",
                                      "result": {"ok": False, "reason": "error"}})

    def _describe(self, session: _Session) -> dict:
        engine = session.engine
        return {"session_id": session.id, "cwd": str(session.cwd), "running": session.running,
                "pending": list(session.pending.values()),
                "usage": dict(engine.session_token_usage),
                "cost_usd": round(float(engine.session_cost.get("usd", 0.0) or 0.0), 6)}

    async def _close_session(self, session: _Session) -> None:
        self.sessions.pop(session.id, None)
        for future in session.futures.values():
            if not future.done():
                future.set_result("deny")
        if session.running:
            await session.engine.request_abort()
            assert session.run is not None
            await asyncio.gather(session.run, return_exceptions=True)
        await session.engine.aclose()
