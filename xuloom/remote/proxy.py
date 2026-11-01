"""Desktop stdio adapter over the same SSH client used by the CLI."""
import argparse
import asyncio
import base64
import json
import sys

from xuloom.remote.config import resolve, target
from xuloom.remote.ssh import SSHClient, RemoteDisconnected


class SSHBridge:
    def __init__(self, identity, binding, machine, emit):
        self.identity, self.binding, self.machine = identity, binding, machine
        self.emit = emit
        self.client = None
        self.lock = asyncio.Lock()
        self.disconnected = False
        self.attachment_cancel = None

    def event(self, event):
        if event["type"] == "remote_disconnected":
            self.disconnected = True
            event = {**event, "type": "engine_exit"}
        self.emit(event)

    async def connect(self):
        async with self.lock:
            if self.disconnected: raise RemoteDisconnected("远端连接已中断；请重新选择会话核对历史后继续")
            if self.client is None:
                profile = resolve(self.identity)
                if self.binding is not None and target(profile) != target(self.binding):
                    raise ValueError("SSH 连接目标已更改；旧会话不会自动改投新主机或目录。请恢复原连接设置。")
                client = SSHClient(profile, self.event)
                try:
                    info = await client.start()
                    if self.machine is not None and any(info.get(key) != self.machine.get(key) for key in ("hostname", "platform", "cwd")):
                        raise ValueError("远端机器或目录与已保存的会话不一致；未恢复或执行旧任务")
                except BaseException:
                    await client.aclose()
                    raise
                self.client = client
            return self.client

    async def dispatch(self, method, params):
        if method == "shutdown":
            await self.close(); return {"closed": True}
        client = await self.connect()
        if method == "remote.connection":
            return {"profile": client.profile, "machine": client.info}
        if method == "stop" and self.attachment_cancel is not None: self.attachment_cancel.set()
        try:
            if method in {"attachments.import", "attachments.download"}:
                from xuloom.remote import attachments
                if self.attachment_cancel is not None: raise ValueError("An attachment transfer is already running")
                cancelled = self.attachment_cancel = asyncio.Event()
                try:
                    if method == "attachments.import":
                        return await attachments.upload(client, params.get("files"), params.get("ids"), params.get("existing", []), cancelled=cancelled)
                    return await attachments.download(client, params.get("id"), params.get("path"), cancelled=cancelled)
                finally: self.attachment_cancel = None
            return await client.request(method, params, timeout=180 if method == "mcp.reload" else 60)
        except RemoteDisconnected as error:
            if not self.disconnected: self.event({"type": "remote_disconnected", "message": str(error), "outcome_unknown": True})
            raise

    async def close(self):
        if self.attachment_cancel is not None: self.attachment_cancel.set()
        if self.client: await self.client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--binding")
    parser.add_argument("--machine")
    args = parser.parse_args()
    decode = lambda value: json.loads(base64.b64decode(value, validate=True).decode("utf-8")) if value else None
    binding, machine = decode(args.binding), decode(args.machine)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    from xuloom.desktop.bridge import serve
    asyncio.run(serve(lambda emit: SSHBridge(args.connection, binding, machine, emit)))


if __name__ == "__main__": main()
