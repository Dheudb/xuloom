"""Session-owned MCP and provider relay; no model key enters the external program."""
import asyncio
import copy
from contextlib import asynccontextmanager, contextmanager
import hmac
import json
import re
import socket
import uuid

from xuloom.core.output_limits import model_reservation, ModelBudgetError
from xuloom.core.run_result import PersistenceError

SOURCE_ID = "_xuloom_source_id"
PREFIX = "mcp__xuloom__"
SAFE_INTERNAL = {"ToolSearch", "EndConversation"}
LIMIT = 64 * 1024 * 1024


class LocalHost:
    def __init__(self, engine, state, *, event_callback=None, turn=0):
        self.engine, self.state = engine, state
        self.callback, self.turn = event_callback, turn
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self.tasks, self.calls = set(), {}
        self.execution_lock = asyncio.Lock()
        self.stopping, self.on_interrupt = False, None
        self.started_calls, self.receipts = set(), {}
        self.result_projection = None
        self.failure = None
        self.failed = asyncio.Event()
        self.usage = {name: 0 for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        from xuloom.agents.collaboration import DELEGATION_BUDGET
        from xuloom.features.goals import CURRENT_BUDGET
        self.delegation, self.goal = DELEGATION_BUDGET.get(), engine.tool_context.get("goal_budget") or CURRENT_BUDGET.get()

    @contextmanager
    def budgets(self):
        from xuloom.agents.collaboration import DELEGATION_BUDGET
        from xuloom.features.goals import CURRENT_BUDGET
        a, b = DELEGATION_BUDGET.set(self.delegation), CURRENT_BUDGET.set(self.goal)
        try:
            yield
        finally:
            CURRENT_BUDGET.reset(b); DELEGATION_BUDGET.reset(a)

    def tools(self):
        return self.engine.available_tools

    def catalog(self):
        # Cache eligible definitions in the CLI before activation. MCP's change
        # notification has no acknowledgement and can race its next model turn.
        # The relay still exposes only native active tools; execution uses tools().
        return {**self.engine.latent_tools, **self.tools()}

    def fail(self, reason):
        self.failure = self.failure or reason
        self.failed.set()

    async def checkpoint(self):
        callback = self.engine.tool_context.get("checkpoint_model_budget")
        if callback is not None:
            await callback()
        if not self.engine.persist_session_state():
            raise PersistenceError("External execution checkpoint could not be saved; no new operation was permitted")

    async def __aenter__(self):
        try:
            import uvicorn
            from mcp.server.lowlevel import Server
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
            from mcp.server.transport_security import TransportSecuritySettings
            from mcp import types
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Route
        except ImportError as error:
            raise ValueError("External agents require the optional MCP runtime; install xuloom[mcp]") from error
        self.mcp = Server("xuloom")

        @self.mcp.list_tools()
        async def listing():
            rows = []
            for name, tool in self.catalog().items():
                schema = copy.deepcopy(tool.input_schema.model_json_schema())
                schema.setdefault("properties", {})[SOURCE_ID] = {"type": "string"}
                rows.append(types.Tool(name=name, description=tool.description, inputSchema=schema,
                    annotations=types.ToolAnnotations(readOnlyHint=bool(getattr(tool, "is_read_only", False)))))
            return rows

        @self.mcp.call_tool()
        async def call(name, arguments):
            values = copy.deepcopy(arguments or {})
            identity = values.pop(SOURCE_ID, None)
            expected = self.calls.get(identity)
            if expected is None or expected != (name, values) or name not in self.tools():
                raise ValueError("This tool invocation has no matching owned model call")
            if self.failure or self.stopping or self.engine.abort_event.is_set():
                raise ValueError("The owning execution has stopped")
            # External MCP calls do not pass through the native batch partitioner.
            # Serialize this boundary so mutations and duplicate transports cannot race.
            async with self.execution_lock:
                if self.failure or self.stopping or self.engine.abort_event.is_set():
                    raise ValueError("The owning execution has stopped")
                before = set(self.catalog())
                try:
                    with self.budgets():
                        self.started_calls.add(identity)
                        result = await self.engine._execute_tool_wrapper({"id": "external-" + identity,
                            "name": name, "input": values}, lambda _: None, turn=self.turn, event_callback=self.callback)
                except PersistenceError:
                    self.fail("persistence_error")
                    raise
            content = []
            for item in result["content"] if isinstance(result["content"], list) else [{"type":"text","text":str(result["content"])}]:
                if item.get("type") == "text": content.append(types.TextContent(type="text", text=item.get("text", "")))
                elif item.get("type") == "image" and item.get("source", {}).get("type") == "base64":
                    source = item["source"]
                    content.append(types.ImageContent(type="image", data=source["data"], mimeType=source["media_type"]))
                else:
                    raise ValueError("External tool content is not supported; the operation was not replayed")
            if before != set(self.catalog()):
                await self.mcp.request_context.session.send_tool_list_changed()
            from .runtime import tool_result_digest
            blocks, failed = [item.model_dump() for item in content], result.get("is_error", False)
            if self.result_projection:
                try:
                    blocks, digest = self.result_projection(name, blocks, failed)
                    content = [types.TextContent(**block) if block['type'] == 'text' else types.ImageContent(**block) for block in blocks]
                except Exception:
                    self.fail('external_content_error')
                    raise
            else:
                digest = tool_result_digest(blocks, failed)
            self.receipts[identity] = (name, digest)
            return types.CallToolResult(content=content, isError=result.get("is_error", False))

        manager = StreamableHTTPSessionManager(self.mcp, json_response=False, stateless=False,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*"], allowed_origins=[]),
            max_request_body_size=LIMIT)
        @asynccontextmanager
        async def lifespan(_):
            async with manager.run(): yield
        owner = self
        class MCPRoute:
            async def __call__(self, scope, receive, send):
                await manager.handle_request(scope, receive, send)
        core = Starlette(routes=[Route("/mcp", MCPRoute(), methods=["GET", "POST", "DELETE"]),
            Route("/v1/messages", self.messages, methods=["POST"]),
            Route("/v1/chat/completions", self.chat_completions, methods=["POST"]),
            Route("/v1/responses", self.responses, methods=["POST"]),
            Route("/v1/messages/count_tokens", self.count_tokens, methods=["POST"])], lifespan=lifespan)
        async def application(scope, receive, send):
            if scope["type"] == "http":
                # Some installed clients use the explicit owned loopback proxy
                # even for its own URL. It is not an open forwarding proxy.
                if scope.get("path", "").startswith(("http://", "https://")):
                    from urllib.parse import urlsplit
                    target = urlsplit(scope["path"])
                    if target.scheme + "://" + target.netloc != owner.url:
                        await JSONResponse({"error":"Unowned destination"}, status_code=403)(scope, receive, send); return
                    scope = {**scope, "path": target.path, "raw_path": target.path.encode(), "query_string": target.query.encode()}
                headers = dict(scope.get("headers", []))
                key = headers.get(b"x-api-key") or headers.get(b"authorization", b"").removeprefix(b"Bearer ")
                if headers.get(b"origin") or not hmac.compare_digest(key, owner.token.encode()):
                    await JSONResponse({"error":"Unauthorized owned connection"}, status_code=401)(scope, receive, send)
                    return
            await core(scope, receive, send)
        class ServerWithoutSignals(uvicorn.Server):
            @contextmanager
            def capture_signals(self): yield
        self.socket = socket.socket(); self.socket.bind(("127.0.0.1", 0))
        self.url = "http://127.0.0.1:" + str(self.socket.getsockname()[1])
        self.server = ServerWithoutSignals(uvicorn.Config(application, log_level="critical", access_log=False, lifespan="on", timeout_graceful_shutdown=3))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(500):
            if self.server.started: return self
            if self.server_task.done():
                await self.server_task
                raise RuntimeError("External tool host could not start")
            await asyncio.sleep(.01)
        await self.__aexit__(None, None, None)
        raise RuntimeError("External tool host startup timed out")

    async def __aexit__(self, exception_type, exception, traceback):
        self.stopping = True
        storage_error = None
        if isinstance(exception, asyncio.CancelledError) and self.on_interrupt is not None:
            try:
                await asyncio.wait_for(self.on_interrupt(), 10)
            except PersistenceError as error:
                storage_error = error
            except (Exception, asyncio.CancelledError):
                pass  # No confirmed checkpoint: the sampler retains outcome_unknown.
        tasks = list(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.server_task), 5)
        except asyncio.TimeoutError:
            self.server_task.cancel(); await asyncio.gather(self.server_task, return_exceptions=True)
            raise RuntimeError("External tool host cleanup could not be confirmed")
        finally:
            self.socket.close()
        if storage_error is not None: raise storage_error

    async def read_payload(self, request):
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > LIMIT: raise ValueError("External request is too large")
        return json.loads(data)

    async def payload(self, request, protocol="anthropic"):
        value = await self.read_payload(request)
        if not isinstance(value, dict) or value.get("model") != self.engine.primary_model:
            raise ValueError("External request changed its frozen model")
        tools, messages = value.get("tools", []), value.get("messages")
        if not isinstance(tools, list) or not isinstance(messages, list) or not messages:
            raise ValueError("External request has invalid tools or messages")
        if value.get("mcp_servers") or any(key in value for key in ("functions", "function_call", "web_search_options", "audio", "modalities")):
            raise ValueError("Provider-hosted MCP is outside the owned tool boundary")
        visible, catalog, active = [], self.catalog(), self.tools()
        for entry in tools:
            tool = entry
            if protocol == "openai":
                if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                    raise ValueError("Provider-hosted tools are outside the owned tool boundary")
                tool = tool["function"]
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("Invalid external tool definition")
            if tool.get("type", "custom") != "custom":
                raise ValueError("Provider-hosted tools are outside the owned tool boundary")
            name = tool.get("name", "")
            if name not in SAFE_INTERNAL and (not name.startswith(PREFIX) or name[len(PREFIX):] not in catalog):
                raise ValueError("The external program exposed an uncontrolled tool")
            if name in SAFE_INTERNAL or name[len(PREFIX):] in active:
                visible.append(entry)
        value["tools"] = visible
        for message in messages:
            roles = {"assistant", "user"} if protocol == "anthropic" else {"assistant", "user", "system", "developer", "tool"}
            if not isinstance(message, dict) or message.get("role") not in roles or (not isinstance(message.get("content"), (str, list)) and not (protocol == "openai" and message.get("role") == "assistant" and message.get("tool_calls"))):
                raise ValueError("Invalid external message")
            if protocol == "openai":
                for call in message.get("tool_calls", []):
                    arguments = json.loads(call["function"]["arguments"])
                    if not isinstance(arguments, dict): raise ValueError("Invalid external tool arguments")
                    arguments.pop(SOURCE_ID, None)
                    call["function"]["arguments"] = json.dumps(arguments)
            if isinstance(message.get("content"), list):
                for block in message["content"]:
                    if not isinstance(block, dict): raise ValueError("Invalid external content block")
                    if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                        block["input"].pop(SOURCE_ID, None)
        return value

    def failure_response(self, status, reason):
        from starlette.responses import JSONResponse
        return JSONResponse({"type":"error","error":{"type":"api_error", "message":reason}}, status_code=status)

    async def count_tokens(self, request):
        try:
            if self.engine.model_provider != "anthropic" or self.failure or self.stopping or self.engine.abort_event.is_set():
                raise ValueError("Token counting is unavailable for this owned execution")
            value = await self.payload(request)
            model, messages = value.pop("model"), value.pop("messages")
            result = await self.engine.client.with_options(max_retries=0).messages.count_tokens(model=model, messages=messages, extra_body=value)
            from starlette.responses import JSONResponse
            return JSONResponse(result.model_dump())
        except Exception:
            return self.failure_response(400, "Configured token counting endpoint is unavailable")

    async def messages(self, request):
        return await self.relay(request, "anthropic")

    async def chat_completions(self, request):
        return await self.relay(request, "openai")

    async def responses(self, request):
        return await self.relay(request, "responses")

    async def relay(self, request, protocol):
        from starlette.responses import StreamingResponse
        if self.failure or self.stopping or self.engine.abort_event.is_set():
            return self.failure_response(409, "The owning execution has stopped; no new model request was permitted")
        try:
            if self.engine.model_provider != ("openai" if protocol == "responses" else protocol): raise ValueError("Changed provider protocol")
            if protocol == "responses":
                from .responses import payload
                if self.state.get("backend") != "codex": raise ValueError("The owned backend does not use Responses")
                value = payload(self, await self.read_payload(request))
            else: value = await self.payload(request, protocol)
            maximum = value.get("max_output_tokens", self.maximum) if protocol == "responses" else value.get("max_completion_tokens", value.get("max_tokens")) if protocol == "openai" else value.get("max_tokens")
            if type(maximum) is not int or maximum <= 0 or (protocol == "openai" and (type(value.get("n", 1)) is not int or value.get("n", 1) != 1)):
                raise ValueError("Invalid output grant request")
            if type(value.get("stream", False)) is not bool: raise ValueError("Invalid streaming mode")
        except (ValueError, KeyError, TypeError):
            self.fail("protocol_error")
            return self.failure_response(400, "External request does not match its controlled capability")
        queue, ready = asyncio.Queue(64), asyncio.get_running_loop().create_future()
        if protocol == "responses":
            from .responses import forward
            execution = forward(self, value, queue, ready)
        elif protocol == "openai":
            from .openai import forward
            execution = forward(self, value, queue, ready)
        else:
            execution = self.forward(value, request.headers, queue, ready)
        task = asyncio.create_task(execution)
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        try:
            status, content_type = await ready
        except BaseException:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
            raise
        if status != 200:
            await task
            return self.failure_response(status, "Configured provider request failed: " + str(self.failure))
        async def drain():
            try:
                while True:
                    if task.done():
                        while not queue.empty(): yield queue.get_nowait()
                        break
                    pending = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait({pending, task}, return_when=asyncio.FIRST_COMPLETED)
                        if pending in done: yield pending.result()
                    finally:
                        if not pending.done(): pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                await task
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(drain(), media_type=content_type)

    def reasoning_options(self, body, maximum):
        """The frozen native model controls override the external CLI's defaults."""
        from xuloom.core.reasoning import request_options
        if self.engine.reasoning_profile is not None:
            for key in ("thinking", "reasoning_effort", "reasoning", "enable_thinking", "chat_template_kwargs"):
                body.pop(key, None)
            if isinstance(body.get("output_config"), dict):
                body["output_config"].pop("effort", None)
                if not body["output_config"]: body.pop("output_config")
            options = request_options(self.engine.reasoning_profile, self.engine.model_provider,
                effort_override=self.engine.reasoning_effort, max_tokens=maximum)
            body.update(options.pop("extra_body", {})); body.update(options)
        elif self.engine.reasoning_effort and self.engine.model_provider == "openai":
            body["reasoning_effort"] = self.engine.reasoning_effort

    async def grant_tool(self, block):
        name, identity = block.get("name", ""), block.get("id")
        if name in SAFE_INTERNAL: return block
        if not name.startswith(PREFIX) or name[len(PREFIX):] not in self.tools() or not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,180}", identity):
            raise ValueError("Provider returned an uncontrolled tool call")
        if identity in self.state["seen_tool_ids"]:
            raise ValueError("Provider repeated a tool identity; the operation was not replayed")
        values = copy.deepcopy(block.get("input"))
        if not isinstance(values, dict): raise ValueError("Tool input is incomplete")
        values.pop(SOURCE_ID, None)
        self.state["seen_tool_ids"].append(identity)
        await self.checkpoint()
        self.calls[identity] = (name[len(PREFIX):], copy.deepcopy(values))
        values[SOURCE_ID] = identity
        return {**block, "input": values}

    async def forward(self, value, headers, queue, ready):
        def frame(event):
            return ("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
        try:
            with self.budgets():
                async with model_reservation(self.engine, min(value["max_tokens"], getattr(self, "maximum", value["max_tokens"]))) as grant:
                    await self.checkpoint()
                    body = copy.deepcopy(value)
                    model, messages = body.pop("model"), body.pop("messages")
                    body.pop("max_tokens"); streamed = body.pop("stream", False)
                    self.reasoning_options(body, grant["maximum"])
                    extra = {name: headers[name] for name in ("anthropic-beta", "anthropic-version") if name in headers}
                    client = self.engine.client.with_options(max_retries=0)
                    async with client.messages.with_streaming_response.create(model=model, messages=messages,
                        max_tokens=grant["maximum"], stream=streamed, extra_body=body, extra_headers=extra) as upstream:
                        ready.set_result((200, "text/event-stream" if streamed else "application/json"))
                        usage = {}
                        if not streamed:
                            data = json.loads(await upstream.text())
                            data["content"] = [await self.grant_tool(block) if block.get("type") == "tool_use" else block for block in data.get("content", [])]
                            usage.update(data.get("usage", {})); await queue.put(json.dumps(data).encode())
                        else:
                            pending, fragments, stopped = {}, {}, False
                            async for line in upstream.iter_lines():
                                if not line.startswith("data:"): continue
                                event = json.loads(line[5:].strip()); kind, index = event.get("type"), event.get("index")
                                if kind == "error": raise ValueError("Provider stream failed")
                                if kind == "message_start": usage.update(event.get("message", {}).get("usage", {}))
                                if kind == "message_delta": usage.update(event.get("usage", {}))
                                if kind == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
                                    pending[index], fragments[index] = event["content_block"], []; continue
                                if index in pending:
                                    if kind == "content_block_delta":
                                        fragments[index].append(event["delta"]["partial_json"]); continue
                                    if kind == "content_block_stop":
                                        block = pending.pop(index)
                                        text = "".join(fragments.pop(index))
                                        block["input"] = json.loads(text) if text else block.get("input", {})
                                        block = await self.grant_tool(block)
                                        await queue.put(frame({"type":"content_block_start","index":index,"content_block":{**block,"input":{}}}))
                                        await queue.put(frame({"type":"content_block_delta","index":index,"delta":{"type":"input_json_delta","partial_json":json.dumps(block["input"])}}))
                                if kind == "message_stop": stopped = True
                                await queue.put(frame(event))
                            if not stopped or pending: raise ValueError("Provider ended with an incomplete response")
                        for key in self.usage: self.usage[key] += int(usage.get(key, 0) or 0)
                        grant["reported"] = int(usage.get("output_tokens", 0) or 0)
        except asyncio.CancelledError:
            if not ready.done(): ready.cancel()
            raise
        except Exception as error:
            self.fail("budget_exhausted" if isinstance(error, ModelBudgetError) else "persistence_error" if isinstance(error, PersistenceError) else "provider_error")
            if not ready.done(): ready.set_result((getattr(error, "status_code", None) or 400, "application/json"))
            else: await queue.put(frame({"type":"error","error":{"type":"api_error","message":"Owned provider stream interrupted"}}))
