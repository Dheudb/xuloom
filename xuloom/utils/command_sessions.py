"""Owned command processes with UTF-8 stdin and lossless, paged disk output."""
import asyncio
import codecs
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from xuloom.utils.shell import output_encoding


class CommandSessions:
    OUTPUT_LIMIT = 50 * 1024 * 1024
    OUTPUT_DRAIN_TIMEOUT = 2

    def __init__(self, root, on_status=None):
        self.root = Path(root).resolve()
        self.records = {}
        self.on_status = on_status
        self.execution_sandbox = None

    def _get(self, identity, owner):
        row = self.records.get(identity)
        if not row or row["owner"] != owner:
            raise ValueError("Unknown command in this agent session")
        return row

    async def start(self, *, argv=None, command=None, cwd, owner, stdin=None, keep_stdin_open=False, timeout_ms=120000):
        if bool(argv) == bool(command):
            raise ValueError("Provide either argv or command")
        if argv and (not isinstance(argv, (list, tuple)) or not all(isinstance(v, str) and "\x00" not in v for v in argv)):
            raise ValueError("argv must contain strings without NUL")
        self.root.mkdir(parents=True, exist_ok=True)
        identity = "cmd-" + uuid.uuid4().hex
        options = {"cwd": cwd, "stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                   "stderr": asyncio.subprocess.PIPE, "env": {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        launch = None
        try:
            if self.execution_sandbox and self.execution_sandbox.enabled:
                launch = self.execution_sandbox.prepare(argv=argv, command=command, cwd=cwd, timeout_ms=timeout_ms)
                process = await asyncio.create_subprocess_exec(*launch.argv, **options)
            else:
                process = await (asyncio.create_subprocess_exec(*argv, **options) if argv else asyncio.create_subprocess_shell(command, **options))
        except BaseException:
            if launch:
                launch.close()
            raise
        row = {"id": identity, "owner": owner, "cwd": str(Path(cwd).resolve()), "argv": argv,
               "command": command, "status": "running", "exit_code": None, "started_at": int(time.time() * 1000),
               "stdin_open": True, "process": process, "input_lock": asyncio.Lock(), "sandbox_launch": launch}
        self.records[identity] = row
        async def pump(stream, name):
            size = 0
            killer = None
            try:
                with (self.root / (identity + "." + name)).open("wb") as output:
                    while True:
                        chunk = await stream.read(65536)
                        if not chunk:
                            break
                        if killer is not None:
                            continue  # Drain the pipe while the owned process tree is stopping.
                        if size + len(chunk) > self.OUTPUT_LIMIT:
                            row["status"] = "output_limit"
                            killer = asyncio.create_task(self._kill(row))
                            continue
                        output.write(chunk)
                        output.flush()
                        size += len(chunk)
            except OSError as error:
                row["status"] = "storage_error"
                row["output_error"] = str(error)
                killer = killer or asyncio.create_task(self._kill(row))
                while await stream.read(65536):
                    pass
            finally:
                if killer:
                    await killer
        pumps = [asyncio.create_task(pump(process.stdout, "stdout")), asyncio.create_task(pump(process.stderr, "stderr"))]
        async def wait_exit():
            # Process.wait() may wait for inherited pipes after the actual child has exited.
            while process.returncode is None:
                await asyncio.sleep(.01)
        async def monitor():
            try:
                await asyncio.wait_for(wait_exit(), timeout_ms / 1000)
            except asyncio.TimeoutError:
                row["status"] = "timed_out"
                await self._kill(row)
            except asyncio.CancelledError:
                row["status"] = "interrupted"
                await self._kill(row)
                raise
            finally:
                try:
                    failures = await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), self.OUTPUT_DRAIN_TIMEOUT)
                except asyncio.TimeoutError:
                    failures = []
                    row["output_complete"] = False
                    row["output_error"] = "The process exited but inherited output pipes did not close; output may be incomplete."
                    if row["status"] == "running":
                        row["status"] = "output_incomplete"
                    transport = getattr(process, "_transport", None)
                    if transport is not None:
                        transport.close()
                if any(isinstance(error, BaseException) for error in failures):
                    row["status"] = "storage_error"
                    row["output_error"] = "; ".join(str(error) for error in failures if isinstance(error, BaseException))
                row["exit_code"] = process.returncode
                row["stdin_open"] = False
                if row["status"] == "running":
                    row["status"] = "completed" if process.returncode == 0 else "failed"
                row["finished_at"] = int(time.time() * 1000)
                if launch:
                    launch.close()
                    if launch.cleanup_issues:
                        row["cleanup_issues"] = launch.cleanup_issues
                try:
                    self._save(row)
                except OSError as error:
                    row["status"] = "storage_error"
                    row["persistence_error"] = str(error)
        row["monitor"] = asyncio.create_task(monitor())
        try:
            await self.write(identity, owner, stdin or "", eof=not keep_stdin_open)
        except BaseException:
            await self.cancel(identity, owner)
            raise
        try:
            self._save(row)
        except BaseException:
            await self.cancel(identity, owner)
            raise
        return identity

    async def _kill(self, row):
        process = row["process"]
        if process.returncode is not None:
            return
        if row.get("sandbox_launch"):
            row["sandbox_launch"].cancel()
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), 25)
                return
            except asyncio.TimeoutError:
                pass
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def write(self, identity, owner, text="", eof=False):
        row = self._get(identity, owner)
        async with row["input_lock"]:
            if row["status"] != "running" or not row["stdin_open"]:
                raise ValueError("Command stdin is closed")
            stream = row["process"].stdin
            try:
                if text:
                    stream.write(text.encode("utf-8"))
                    await asyncio.wait_for(stream.drain(), 30)
                if eof:
                    stream.close()
                    row["stdin_open"] = False
            except (BrokenPipeError, ConnectionResetError):
                row["stdin_open"] = False
                if text:
                    raise ValueError("Command exited before accepting stdin")

    async def poll(self, identity, owner, wait_ms=0):
        row = self._get(identity, owner)
        task = row.get("monitor")
        if task and not task.done() and wait_ms:
            await asyncio.wait([task], timeout=wait_ms / 1000)
        return self.snapshot(row)

    def read(self, identity, owner, stream="stdout", offset=0, max_bytes=4000):
        if stream not in {"stdout", "stderr"} or offset < 0 or max_bytes < 4:
            raise ValueError("Invalid output stream, offset or page size")
        row = self._get(identity, owner)
        path = self.root / (identity + "." + stream)
        size = path.stat().st_size if path.exists() else 0
        if offset > size:
            raise ValueError("Output offset exceeds current output size")
        data = b""
        encoding = "utf-8"
        if path.exists():
            with path.open("rb") as handle:
                encoding = output_encoding(handle.read(65536))
                handle.seek(offset)
                data = handle.read(max_bytes)
        decoder = codecs.getincrementaldecoder(encoding)("replace")
        terminal = row["status"] != "running" and offset + len(data) == size
        text = decoder.decode(data, final=terminal)
        consumed = len(data) - len(decoder.getstate()[0])
        return {"stream": stream, "encoding": encoding, "text": text, "offset": offset, "next_offset": offset + consumed,
                "total_bytes": size, "eof": row["status"] != "running" and offset + consumed == size,
                "has_more": offset + consumed < size}

    async def cancel(self, identity, owner):
        row = self._get(identity, owner)
        if row.get("process") and row["process"].returncode is None:
            if row["status"] == "running":
                row["status"] = "cancelled"
            await self._kill(row)
        if row.get("monitor"):
            await asyncio.shield(row["monitor"])
        return self.snapshot(row)

    @staticmethod
    def snapshot(row):
        return {k: v for k, v in row.items() if k not in {"process", "monitor", "input_lock", "sandbox_launch"}}

    def _save(self, row):
        from xuloom.core.storage import atomic_json
        try:
            atomic_json(self.root / (row["id"] + ".json"), self.snapshot(row))
        except OSError as error:
            row["status"] = "storage_error"
            row["persistence_error"] = str(error)
            raise
        if self.on_status:
            try:
                self.on_status({k: row[k] for k in ("id", "owner", "status", "exit_code")})
            except Exception as error:
                row["notification_error"] = str(error)

    def restore(self):
        if not self.root.exists():
            return
        for path in self.root.glob("cmd-*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                if row.get("id") != path.stem or not path.stem.startswith("cmd-") or len(path.stem) != 36:
                    continue
                if row.get("status") == "running":
                    row["status"] = "interrupted"
                row["stdin_open"] = False
                self.records[row["id"]] = row
            except (ValueError, OSError):
                continue

    async def close(self, owner=None):
        results = await asyncio.gather(*(self.cancel(row["id"], row["owner"]) for row in list(self.records.values())
            if (owner is None or row["owner"] == owner) and
            (row.get("process") and row["process"].returncode is None or row.get("monitor") and not row["monitor"].done())), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
