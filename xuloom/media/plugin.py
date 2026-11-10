"""Bundled ``media`` plugin: image/video/audio generation through configured queue APIs.

Everything media generation adds to Xuloom is registered here through the
public :class:`xuloom.plugins.PluginAPI`: the ``media_tool`` (latent until the
model searches for it), the ``xuloom media ...`` CLI commands and the
``media.*`` desktop RPC methods. Disable it with ``xuloom plugins disable media``.
"""
import asyncio
import uuid

NAME = "media"

_CONFIG_METHODS = ("media.connections", "media.save", "media.remove", "media.models", "media.saveModel", "media.removeModel")


def register(api):
    from xuloom.media import cli
    from xuloom.media.tool import MediaTool
    api.add_tool(MediaTool(), latent=True)
    api.add_cli_command("media", cli.configure, cli.dispatch,
                        help="Configure generation APIs and inspect durable media receipts")
    for method in _CONFIG_METHODS:
        api.add_rpc_method(method, _config_rpc(method), settings=method not in {"media.connections", "media.models"})
    api.add_rpc_method("media.jobs", _jobs_rpc)
    api.add_rpc_method("media.control", _control_rpc, operation=True)


def _config_rpc(method):
    async def handler(params, context):
        from xuloom.media import config
        if method == "media.connections":
            return {"connections": config.public_connections()}
        if method == "media.models":
            return {"models": config.public_models()}
        context.require_idle()
        if method == "media.saveModel":
            return {"models": await asyncio.to_thread(config.save_model, params.get("model"))}
        if method == "media.removeModel":
            if params.get("confirm") is not True:
                raise ValueError("请确认移除生成模型")
            return {"models": await asyncio.to_thread(config.remove_model, params.get("connectionId"), params.get("modelId"))}
        if method == "media.remove" and params.get("confirm") is not True:
            raise ValueError("请确认移除媒体连接；这不会取消供应商已有任务")
        if method == "media.save":
            return {"connections": await asyncio.to_thread(config.save, params.get("connection"))}
        return {"connections": await asyncio.to_thread(config.remove, params.get("id"))}
    return handler


async def _jobs_rpc(params, context):
    from xuloom.media.jobs import listing
    sid = context.session_id
    return {"jobs": await asyncio.to_thread(listing, {"session_cwd": str(context.cwd), "session_id": sid}) if sid else []}


async def _control_rpc(params, context):
    context.require_idle()
    if params.get("action") not in {"status", "cancel"} or not context.session_id:
        raise ValueError("请打开已有会话，并选择查询或取消已有媒体任务")
    engine = await context.engine(write=True)
    from xuloom.media.jobs import load
    load(engine.tool_context, params.get("jobId"))
    context.start_operation("media", lambda: _control(engine, context, params["action"], params["jobId"]))
    return {"started": True}


async def run_control(engine, action, identity, wait_ms=0):
    """Query or cancel a saved job through ``media_tool`` (same permissions and receipts as the model)."""
    from xuloom.media.jobs import load, public
    if "media_tool" not in engine.activate_tools(["media_tool"]):
        raise ValueError("媒体插件未加载或在当前隔离模式下不可用")
    receipt = await engine._execute_tool_wrapper({"id": "media-control-" + uuid.uuid4().hex, "name": "media_tool",
        "input": {"action": action, "job_id": identity, "wait_ms": wait_ms}}, lambda _: None)
    return receipt, public(load(engine.tool_context, identity))


async def _control(engine, context, action, identity):
    context.emit({"type": "operation_started", "operation": "media", "jobId": identity})
    try:
        receipt, record = await run_control(engine, action, identity)
        context.emit({"type": "media_job_changed", "job": record})
        if receipt["is_error"]:
            context.emit({"type": "operation_error", "message": record.get("error") or "媒体操作未执行；请检查权限与已有任务记录。"})
    except asyncio.CancelledError:
        context.emit({"type": "operation_error", "message": "已停止本机操作；供应商任务可能仍在运行，可继续查询或请求取消。"})
    except Exception as error:
        context.emit({"type": "operation_error", "message": str(error)})
    finally:
        context.emit({"type": "operation_finished", "operation": "media"})
