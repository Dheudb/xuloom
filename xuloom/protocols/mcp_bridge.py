"""MCP connections owned and closed by one asyncio task."""
import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import urlsplit
import ipaddress
import copy
import httpx

from xuloom.protocols.mcp_config import MCPConfigStore, validate_server
from xuloom.context.tool_results import build_tool_result

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client, get_default_environment
    from mcp.client.streamable_http import streamable_http_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


def runtime_info():
    return {"available": MCP_AVAILABLE, "python": sys.version.split()[0], "executable": sys.executable,
            "message": "" if MCP_AVAILABLE else "MCP 依赖不可用。桌面用户请重新解压完整程序包；源码环境需 Python 3.10+ 并安装 xuloom[mcp]。"}


def connection_error(exc):
    # SDK exceptions can contain request URLs/headers. Do not send them to the UI/model.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "连接超时；检查网络、启动命令及服务配置。"
    if isinstance(exc, FileNotFoundError):
        return "找不到启动程序；检查服务所需的 npx、uvx 或其他运行环境。"
    return "MCP 连接或请求失败（" + type(exc).__name__ + "）；检查运行环境、地址及凭据。"


class MCPToolWrapper:
    def __init__(self, server_name, session, mcp_tool):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", server_name + "_" + mcp_tool.name)
        self.name = safe[:64]
        self.server_name = server_name
        self.description = mcp_tool.description or "MCP tool from " + server_name
        self.mcp_tool_name = mcp_tool.name
        self.session = session
        # jsonschema/referencing are dependencies of the optional MCP SDK.
        # Never fetch a schema URL on behalf of a connected tool.
        from jsonschema.validators import validator_for
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
        self.raw_schema = copy.deepcopy(mcp_tool.inputSchema)
        if isinstance(self.raw_schema, dict) and "$schema" in self.raw_schema and validator_for(self.raw_schema, default=None) is None:
            raise ValueError("Unsupported MCP JSON Schema dialect")
        validator = validator_for(self.raw_schema)
        validator.check_schema(self.raw_schema)
        def offline(uri):
            raise NoSuchResource(ref=uri)
        self._validator = validator(self.raw_schema, registry=Registry(retrieve=offline))
        self.context = {}
        self.is_read_only = False
        self.risk_level = "high"

    def get_tool_definition(self):
        return {"name": self.name, "description": self.description, "input_schema": copy.deepcopy(self.raw_schema)}

    def validate_input(self, **kwargs):
        try:
            error = next(self._validator.iter_errors(kwargs), None)
        except Exception:
            return "MCP input schema could not be evaluated locally. Ask the server to provide a valid, self-contained schema; no remote operation was executed."
        if error is not None:
            # ValidationError.message can embed the entire argument, including
            # credentials or long file contents. Report paths/constraints only.
            location = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
            return f"Invalid MCP arguments at {location[:160]}: constraint {str(error.validator)[:40]}. Use the declared input schema; no remote operation was executed."
        return None

    def is_read_only_call(self, **kwargs):
        return False

    def is_concurrency_safe_call(self, **kwargs):
        return False

    def get_risk_level(self, **kwargs):
        return self.risk_level

    def build_permission_summary(self, **kwargs):
        return "MCP tool '" + self.name + "' requested.\n" + "\n".join(str(k) + ": " + str(v)[:160] for k, v in list(kwargs.items())[:4])

    async def __call__(self, **kwargs):
        error = self.validate_input(**kwargs)
        if error:
            from xuloom.protocols.dsml import ToolProtocolError
            raise ToolProtocolError(error)
        try:
            result = await self.session.call_tool(self.mcp_tool_name, arguments=kwargs)
            blocks = []
            sources = []
            visual = False
            for block in result.content:
                if getattr(block, "type", "") == "resource_link":
                    sources.append({"url": str(block.uri), "title": str(getattr(block, "title", None) or block.name), "kind": "resource"})
                if getattr(block, "type", "") == "text":
                    blocks.append({"type": "text", "text": block.text})
                elif getattr(block, "type", "") == "image":
                    visual = True
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": block.mimeType, "data": block.data}})
                else:
                    raw = block.model_dump(mode="json", by_alias=True) if hasattr(block, "model_dump") else vars(block)
                    blocks.append({"type": "text", "text": json.dumps(raw, ensure_ascii=False)})
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                blocks.append({"type": "text", "text": "Structured content:\n" + json.dumps(structured, ensure_ascii=False)})
            error = bool(getattr(result, "isError", False))
            return build_tool_result(ok=not error, is_error=error, content=blocks if visual else "\n".join(block["text"] for block in blocks),
                metadata={"server": self.server_name, "tool": self.mcp_tool_name, "content_blocks": len(result.content), "sources_in_content": True, "sources": sources})
        except Exception as exc:
            return build_tool_result(ok=False, is_error=True, content=connection_error(exc), metadata={"server": self.server_name})


class MCPBridge:
    def __init__(self, config_file="mcp_servers.json"):
        self.store = MCPConfigStore(config_file)
        self.sessions = {}
        self.connections = []
        self.statuses = {}

    async def load_and_connect(self, only=None, include_disabled=False):
        discovered = {}
        for name, raw in self.store.read(include_skills=True)["mcpServers"].items():
            if only is not None and name != only:
                continue
            if isinstance(raw, dict) and raw.get("enabled", True) is False and not include_disabled:
                self.statuses[name] = {"status": "disabled", "tools": []}
                continue
            if not MCP_AVAILABLE:
                self.statuses[name] = {"status": "error", "error": runtime_info()["message"], "tools": []}
                continue
            ready = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            task = asyncio.create_task(self._connection(name, raw, ready, stop))
            self.connections.append((task, stop))
            tools = await asyncio.shield(ready)
            if any(key in discovered for key in tools):
                self.statuses[name] = {"status": "error", "error": "MCP tool name collision", "tools": []}
                stop.set()
                await task
            else:
                discovered.update(tools)
        return discovered

    async def _connection(self, name, raw, ready, stop):
        # A disconnected transport can cancel its AnyIO scope. Keep each
        # connection on its own owner so one failed service cannot stop another.
        try:
            async with AsyncExitStack() as stack:
                config = validate_server(raw)
                if config["type"] == "stdio":
                    inherited = get_default_environment()
                    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                        if key in os.environ:
                            inherited[key] = os.environ[key]
                    params = StdioServerParameters(command=config["command"], args=config.get("args", []), env={**inherited, **config.get("env", {})})
                    sink = stack.enter_context(open(os.devnull, "w"))
                    channels = await stack.enter_async_context(stdio_client(params, errlog=sink))
                else:
                    hostname = urlsplit(config["url"]).hostname
                    try:
                        local = ipaddress.ip_address(hostname).is_loopback
                    except ValueError:
                        local = hostname == "localhost"
                    client = await stack.enter_async_context(httpx.AsyncClient(headers=config.get("headers", {}), timeout=httpx.Timeout(30, connect=15), trust_env=not local, follow_redirects=False))
                    channels = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
                session = await stack.enter_async_context(ClientSession(channels[0], channels[1], read_timeout_seconds=timedelta(seconds=config["tool_timeout_sec"])))
                # AnyIO cancel scopes must stay on their owning task. The session owns deadlines.
                await session.initialize()
                tools = []
                cursor = None
                for _ in range(20):
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                if cursor:
                    raise ValueError("MCP tool pagination exceeded limit")
                wrappers = [MCPToolWrapper(name, session, tool) for tool in tools]
                names = [tool.name for tool in wrappers]
                if len(names) != len(set(names)):
                    raise ValueError("MCP tool name collision")
                self.sessions[name] = session
                self.statuses[name] = {"status": "connected", "tools": [tool.name for tool in tools]}
                ready.set_result({tool.name: tool for tool in wrappers})
                await stop.wait()
        except BaseException as exc:
            self.statuses[name] = {"status": "error", "error": connection_error(exc), "tools": []}
            if not ready.done():
                ready.set_result({})
        finally:
            self.sessions.pop(name, None)
            if not ready.done():
                ready.set_result({})

    async def cleanup(self):
        for task, stop in self.connections:
            stop.set()
        await asyncio.gather(*(task for task, _ in self.connections), return_exceptions=True)
        self.connections.clear()
        self.sessions.clear()
