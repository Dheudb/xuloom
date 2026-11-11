"""Tool confinement policy, separate from approvals and workspace copies."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

FILE_TOOLS = frozenset({"file_read_tool", "file_write_tool", "file_edit_tool", "glob_tool", "grep_tool", "notebook_edit_tool"})
CONTROL_TOOLS = frozenset({"ultra_answer", "ultra_prepare", "tool_sequence", "agent_tool", "spawn_agent", "ask_user_question_tool", "todo_write_tool", "plan_tool", "goal_report_tool",
    "goal_tool", "schedule_tool", "brief_tool", "tool_search_tool", "skill_tool", "send_message_tool",
    "team_create_tool", "team_delete_tool", "enter_plan_mode", "exit_plan_mode"})
PROCESS_TOOLS = frozenset({"command_tool", "repl_tool"})


class SandboxUnavailable(RuntimeError):
    pass


def detect_backend():
    """The system isolation backend for this host, or ``None`` (workspace mode then fails closed)."""
    if os.name == "nt":
        return "windows-lpac" if ctypes.sizeof(ctypes.c_void_p) == 8 else None
    if sys.platform.startswith("linux"):
        return "linux-bwrap" if shutil.which("bwrap") else None
    if sys.platform == "darwin":
        return "macos-seatbelt" if shutil.which("sandbox-exec") else None
    return None


# Read-only system locations visible inside the Linux sandbox (missing ones are skipped).
LINUX_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt")


# Like the Windows launcher, tool processes get an allowlisted environment: no API keys or tokens.
POSIX_ENVIRONMENT = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME")


def restricted_posix_environment(temporary, environ=None):
    environ = os.environ if environ is None else environ
    result = {key: environ[key] for key in POSIX_ENVIRONMENT if environ.get(key)}
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    result.update(HOME=str(temporary), TMPDIR=str(temporary), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", MPLBACKEND="Agg")
    return result


def _clean_env(environment):
    return ["/usr/bin/env", "-i", *(f"{key}={value}" for key, value in sorted(environment.items()))]


def bwrap_argv(argv, *, workspace, cwd, read_roots, timeout_ms, bwrap="bwrap", timeout_program=None, environment=None):
    """bubblewrap command line: empty root, read-only system and runtime, writable workspace only,
    every namespace (including the network) unshared, killed with its parent."""
    command = [bwrap, "--die-with-parent", "--new-session", "--unshare-all",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]
    for root in LINUX_SYSTEM_ROOTS:
        command += ["--ro-bind-try", root, root]
    for root in read_roots:
        command += ["--ro-bind", str(root), str(root)]
    command += ["--bind", str(workspace), str(workspace), "--chdir", str(cwd), "--"]
    command += _clean_env(environment if environment is not None else restricted_posix_environment("/tmp"))
    if timeout_program and timeout_ms:
        command += [timeout_program, "--kill-after=5", f"{max(1, int(timeout_ms) // 1000)}s"]
    return command + list(argv)


def _sbpl(path):
    """Quote a path as an SBPL string literal."""
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


MACOS_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/etc", "/private/var/db",
                      "/opt/homebrew", "/dev")


def seatbelt_profile(*, workspace, read_roots, temporary):
    """sandbox-exec profile: deny by default, no network, read system/runtime, write workspace and temp only."""
    readable = [*MACOS_SYSTEM_ROOTS, *map(str, read_roots), str(workspace), str(temporary)]
    return "\n".join([
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow file-read-metadata)",
        '(allow file-read* (literal "/") ' + " ".join(f"(subpath {_sbpl(path)})" for path in readable) + ")",
        f"(allow file-write* (subpath {_sbpl(workspace)}) (subpath {_sbpl(temporary)}) "
        '(literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
        '(allow file-ioctl (literal "/dev/tty"))',
    ])


class PreparedLaunch:
    def __init__(self, argv, directory, api=None, event=None, finished=None):
        self.argv, self.directory, self.api, self.event = argv, directory, api, event
        self.finished = finished
        self.cleanup_issues = []

    def cancel(self):
        if self.event:
            self.api.kernel.SetEvent(self.event)

    def close(self):
        if self.event:
            self.api.kernel.CloseHandle(self.event)
            self.event = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None
        if self.finished:
            finished, self.finished = self.finished, None
            self.cleanup_issues = finished()


class ExecutionSandbox:
    def __init__(self, workspace, mode="host"):
        if mode not in {"host", "workspace"}:
            raise ValueError("Execution isolation must be host or workspace")
        self.workspace = str(Path(workspace).resolve())
        self.mode = mode
        self.processes = set()
        self.notify = None

    @property
    def enabled(self):
        return self.mode == "workspace"

    BACKEND_MESSAGES = {
        "windows-lpac": "Windows LPAC: authorized workspace, read-only runtimes, no network capabilities, owned process jobs",
        "linux-bwrap": "Linux bubblewrap (experimental): writable workspace, read-only system and runtime, "
                       "home/credentials not mounted, all namespaces including network unshared",
        "macos-seatbelt": "macOS seatbelt (experimental): deny-by-default profile, writable workspace and temp only, no network",
    }

    def status(self):
        backend = detect_backend()
        from xuloom.security.sandbox_cleanup import pending
        if backend:
            message = self.BACKEND_MESSAGES[backend]
        elif sys.platform.startswith("linux"):
            message = "Install bubblewrap (bwrap) to enable workspace isolation on Linux; workspace mode fails closed"
        elif sys.platform == "darwin":
            message = "sandbox-exec is unavailable; workspace mode fails closed"
        else:
            message = "This platform has no validated system execution backend; workspace mode fails closed"
        return {"mode": self.mode, "available": backend is not None, "backend": backend,
            "experimental": backend in {"linux-bwrap", "macos-seatbelt"},
            "cleanup_issues": pending(self.workspace) if backend == "windows-lpac" else [],
            "network": "blocked-in-tool-processes" if self.enabled else "host",
            "message": message,
            "unsupported_tools": "Browser, LSP, network tools and MCP servers are blocked in workspace mode until confined backends are available"}

    def check_tool(self, name):
        if self.enabled and name not in FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS:
            raise SandboxUnavailable("System isolation blocks this tool: " + name + ". No confined backend is available; it was not executed on the host.")

    def prepare(self, *, argv=None, command=None, cwd=None, timeout_ms=120000, read_roots=()):
        if not self.enabled:
            raise ValueError("A sandbox launch requires workspace execution mode")
        status = self.status()
        if not status["available"]:
            raise SandboxUnavailable(status["message"])
        if status["cleanup_issues"]:
            raise SandboxUnavailable("A previous isolated execution needs permission cleanup. Open execution isolation settings or run xuloom sandbox-cleanup; no new tool was started.")
        cwd = Path(cwd or self.workspace).resolve(strict=True)
        root = Path(self.workspace)
        if not cwd.is_relative_to(root):
            raise SandboxUnavailable("Requested cwd is outside the authorized execution workspace")
        if bool(argv) == bool(command):
            raise ValueError("Provide argv or command")
        package = Path(__file__).resolve().parents[1]
        from xuloom.core.storage import data_directory
        from xuloom.core.config import CONFIG_DIR
        data = data_directory().resolve()
        # Never grant writable access over the trusted harness, credentials or
        # execution records. Package-SID deny ACEs cannot safely override a
        # broad inherited workspace grant in all AppContainer access checks.
        runtime_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), package]
        for parent in Path(sys.prefix).parents:
            if (parent / "Xuloom.exe").is_file():
                runtime_roots.append(parent)
                break
        sensitive = [Path(CONFIG_DIR).resolve(), data / "history", data / "session-locks", data / "request-limits", data / "sandbox-cleanup", data / "mcp-oauth", data / "schedules.json", data / "workbench.json"]
        for trusted in runtime_roots + sensitive:
            if trusted.is_relative_to(root) or root.is_relative_to(trusted):
                raise SandboxUnavailable("The writable workspace overlaps trusted runtime, credentials or session storage. Choose a separate working directory; no tool was started.")
        if status["backend"] != "windows-lpac":
            return self._prepare_posix(status["backend"], argv=argv, command=command, cwd=cwd, root=root, package=package,
                                       runtime_roots=runtime_roots, read_roots=read_roots, timeout_ms=timeout_ms)
        if command:
            argv = [str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"), "/d", "/s", "/c", command]
        else:
            argv = list(argv)
            executable = shutil.which(argv[0])
            if not executable:
                raise FileNotFoundError("Sandbox executable could not be resolved")
            argv[0] = executable
        from xuloom.security.windows_sandbox import WindowsAPI
        api = WindowsAPI()
        event_name = "Local\\Xuloom.Sandbox." + uuid.uuid4().hex
        event = api.check(api.kernel.CreateEventW(None, True, False, event_name))
        directory = tempfile.TemporaryDirectory(prefix="xuloom-launch-")
        candidates = [package, Path(sys.executable).resolve(), Path(getattr(sys, "_base_executable", sys.executable)).resolve()]
        for prefix in {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}:
            candidates.extend(path for path in (prefix / "Lib", prefix / "DLLs") if path.exists())
            candidates.extend(path for pattern in ("python*.dll", "python*.zip", "python*._pth", "vcruntime*.dll", "pyvenv.cfg") for path in prefix.glob(pattern))
        candidates += [Path(p).resolve() for p in read_roots if not Path(p).resolve().is_relative_to(root)]
        executable_path = Path(argv[0]).resolve()
        windows = Path(os.environ["SystemRoot"]).resolve()
        if not executable_path.is_relative_to(windows) and not executable_path.is_relative_to(root):
            # Resolving an executable must never authorize its whole parent
            # directory (which could be the user's home or credential folder).
            candidates.append(executable_path)
        roots = [path for path in dict.fromkeys(candidates) if not any(path != other and path.is_relative_to(other) for other in candidates)]
        spec = {"workspace": self.workspace, "cwd": str(cwd), "argv": argv, "read_roots": [str(path) for path in roots],
            "read_parents": [], "owner_pid": os.getpid(), "cancel_event": event_name, "timeout_ms": timeout_ms,
            "shell_command": command}
        path = Path(directory.name) / "launch.json"
        try:
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        except BaseException:
            api.kernel.CloseHandle(event); directory.cleanup()
            raise
        return PreparedLaunch([sys.executable, "-I", "-B", str(Path(__file__).with_name("windows_sandbox.py")), str(path)], directory, api, event, self._cleanup_finished)

    def _prepare_posix(self, backend, *, argv, command, cwd, root, package, runtime_roots, read_roots, timeout_ms):
        if command:
            argv = ["/bin/sh", "-c", command]
        else:
            argv = list(argv)
            executable = shutil.which(argv[0])
            if not executable:
                raise FileNotFoundError("Sandbox executable could not be resolved")
            argv[0] = executable
        system = LINUX_SYSTEM_ROOTS if backend == "linux-bwrap" else MACOS_SYSTEM_ROOTS
        candidates = [Path(path).resolve() for path in (*runtime_roots, package, sys.executable,
                                                         getattr(sys, "_base_executable", sys.executable))]
        candidates += [Path(p).resolve() for p in read_roots]
        executable_path = Path(argv[0]).resolve()
        if not executable_path.is_relative_to(root):
            # Mount the resolved executable itself, never its whole parent directory.
            candidates.append(executable_path)
        candidates = [path for path in candidates if path.exists() and not path.is_relative_to(root)
                      and not any(path.is_relative_to(Path(base)) for base in system)]
        roots = [path for path in dict.fromkeys(candidates) if not any(path != other and path.is_relative_to(other) for other in candidates)]
        if backend == "linux-bwrap":
            launch_argv = bwrap_argv(argv, workspace=root, cwd=cwd, read_roots=roots, timeout_ms=timeout_ms,
                                     bwrap=shutil.which("bwrap") or "bwrap", timeout_program=shutil.which("timeout"))
            return PreparedLaunch(launch_argv, None)
        directory = tempfile.TemporaryDirectory(prefix="xuloom-launch-")
        temporary = Path(directory.name).resolve()
        profile = seatbelt_profile(workspace=root, read_roots=roots, temporary=temporary)
        return PreparedLaunch([shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec", "-p", profile,
                               *_clean_env(restricted_posix_environment(temporary)), *argv], directory)

    def _cleanup_finished(self):
        issues = self.status()["cleanup_issues"]
        if issues and self.notify:
            self.notify({"type": "log", "text": "系统隔离的权限清理尚未完成；执行结果保留，未重跑任务。请在执行隔离设置中重试清理。记录：" + issues[0]["report_path"]})
        return issues

    async def communicate(self, *, argv=None, command=None, cwd=None, data=None, timeout=120, read_roots=()):
        launch = self.prepare(argv=argv, command=command, cwd=cwd, timeout_ms=int(timeout * 1000), read_roots=read_roots)
        process = None
        try:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = await asyncio.create_subprocess_exec(*launch.argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
            self.processes.add(process)
            output, error = await asyncio.wait_for(process.communicate(data), timeout + 30)
            return process.returncode, output, error
        finally:
            if process and process.returncode is None:
                launch.cancel()
                try:
                    await asyncio.wait_for(process.wait(), 25)
                except asyncio.TimeoutError:
                    process.kill(); await process.wait()
            if process:
                self.processes.discard(process)
            launch.close()

    async def tool(self, name, arguments, context):
        queue = context.get("incremental_write_queue")
        path = arguments.get("file_path") or arguments.get("notebook_path")
        if queue and path and name in {"file_write_tool", "file_edit_tool", "notebook_edit_tool"}:
            return await queue.run(path, name, lambda: self._file_operation(name, arguments, context))
        return await self._file_operation(name, arguments, context)

    async def _file_operation(self, name, arguments, context):
        package_root = str(Path(__file__).resolve().parents[2])
        entry = "import sys;sys.path.insert(0," + repr(package_root) + ");from xuloom.security.worker import main;main()"
        cache = context.get("file_state_cache")
        from xuloom.core.model_inputs import configured_inputs
        model = context.get("request_model", context.get("primary_model", ""))
        contract = context.get("model_input_contract")
        request = {"name": name, "arguments": arguments, "provider": context.get("model_provider"), "read_state": context.get("read_file_state", {}),
            "cache": cache.export_state() if cache else {}, "agent_id": context.get("agent_id"), "session_id": context.get("session_id"),
            "model_input_modalities": contract(model) if callable(contract) else configured_inputs(context.get("model_provider", ""), model, context.get("api_base_url", ""))}
        rg = shutil.which("rg") if name == "grep_tool" else None
        code, output, error = await self.communicate(argv=[sys.executable, "-I", "-B", "-X", "utf8", "-c", entry], read_roots=[Path(rg)] if rg else [],
            data=json.dumps(request, ensure_ascii=False).encode("utf-8"))
        if code:
            raise SandboxUnavailable("Confined tool failed (exit " + str(code) + "): " + error.decode("utf-8", "replace")[-3000:])
        try:
            reply = json.loads(output)
        except (ValueError, UnicodeError):
            raise SandboxUnavailable("Confined tool returned an invalid reply; no host fallback was attempted") from None
        if not reply.get("ok"):
            raise SandboxUnavailable(reply.get("error", "Confined tool failed"))
        context.setdefault("read_file_state", {}).update(reply.get("read_state", {}))
        if cache:
            state = cache.export_state()
            before = request["cache"].get("entries", {})
            state.setdefault("entries", {}).update({path: row for path, row in reply.get("cache", {}).get("entries", {}).items() if before.get(path) != row})
            cache.load_state(state)
        callback = context.get("capability_event")
        for event in reply.get("events", []):
            if callable(callback):
                callback(event)
        return reply["result"]
