import asyncio
import os
import json
from typing import Optional

class LSPBridge:
    """
    Raw socket/stdio JSON-RPC 2.0 protocol wrapper for managing Language Servers.
    Crucial for bypassing basic text grep to achieve genuine AST-level context awareness.
    """
    def __init__(self, command: str, args: list):
        self.command = command
        self.args = args
        self.process: Optional[asyncio.subprocess.Process] = None
        self._msg_id = 0
        self.pending_requests = {}
        self.capabilities = {}
        self._read_task = None
        self.diagnostic_callback = None
        
    def set_diagnostic_callback(self, cb):
        self.diagnostic_callback = cb
        
    async def start(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self._read_task = asyncio.create_task(self._listen())
            
            # Handshake Initialization per LSP spec
            init_res = await self.send_request("initialize", {
                "processId": os.getpid(),
                "rootUri": f"file://{os.getcwd()}",
                "capabilities": {}
            })
            self.capabilities = init_res.get("capabilities", {})
            self.send_notification("initialized", {})
            return True
        except FileNotFoundError:
            print(f"\\n[LSP Warning] Could not find executable '{self.command}'. LSP Features degraded.")
            return False
        except Exception as e:
            print(f"\\n[LSP Error] Failed to spin up language daemon: {e}")
            return False
            
    async def _listen(self):
        """Asynchronous stream parser dealing with raw `Content-Length` sticky packets."""
        try:
            buffer = b""
            while True:
                data = await self.process.stdout.read(4096)
                if not data: break
                buffer += data
                
                while b"Content-Length: " in buffer:
                    if b"\\r\\n\\r\\n" not in buffer:
                        break
                        
                    head, tail = buffer.split(b"\\r\\n\\r\\n", 1)
                    header_str = head.decode('utf-8', errors='replace')
                    
                    content_length = 0
                    for line in header_str.split('\\r\\n'):
                        if line.startswith("Content-Length:"):
                            content_length = int(line.split(":")[1].strip())
                            
                    if len(tail) < content_length:
                        break # Incomplete packet, wait for next drain stream
                        
                    msg_body = tail[:content_length]
                    buffer = tail[content_length:]
                    
                    try:
                        msg = json.loads(msg_body.decode('utf-8'))
                        
                        # 1. Intercept Passive Notifications (Diagnostics Server Push)
                        if "method" in msg and msg["method"] == "textDocument/publishDiagnostics":
                            if self.diagnostic_callback:
                                params = msg.get("params", {})
                                self.diagnostic_callback(params.get("uri"), params.get("diagnostics", []))
                                
                        # 2. Fulfill matching futures
                        elif "id" in msg and msg["id"] in self.pending_requests:
                            future = self.pending_requests.pop(msg["id"])
                            if not future.done():
                                if "error" in msg:
                                    future.set_exception(Exception(msg["error"]))
                                else:
                                    future.set_result(msg.get("result"))
                    except json.JSONDecodeError:
                        pass # Ignore broken responses
                        
        except Exception:
            pass # Socket death handles silently
            
    async def send_request(self, method: str, params: dict, timeout=10.0):
        self._msg_id += 1
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[self._msg_id] = future
        
        req = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": method,
            "params": params
        }
        body = json.dumps(req).encode('utf-8')
        header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode('utf-8')
        
        if not self.process:
            raise RuntimeError("LSP Server offline")
            
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()
        
        return await asyncio.wait_for(future, timeout=timeout)
        
    def send_notification(self, method: str, params: dict):
        if not self.process:
            return
            
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        body = json.dumps(req).encode('utf-8')
        header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode('utf-8')
        self.process.stdin.write(header + body)
        
    async def stop(self):
        if self.process:
            try:
                self.send_notification("exit", {})
                self.process.terminate()
            except Exception:
                pass
            if self._read_task:
                self._read_task.cancel()
