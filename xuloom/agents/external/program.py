"""Resolve an installed program without shell interpolation or downloads."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile

REQUIRED = {"claude": ("--restricted", "--bare", "--tools", "--strict-mcp-config", "--input-format",
            "--output-format", "--include-partial-messages", "--system-prompt", "--session-id", "--resume", "--permission-prompts", "--setting-sources"),
    "pi": ("--mode", "--no-builtin-tools", "--no-extensions", "--no-approve", "--offline", "--no-context-files", "--session", "--session-id", "--session-dir"),
    "codex": ("app-server", "--config", "--disable")}
_probes = {}


def environment(home):
    # The selected provider key lives in the Python relay, never this process.
    result = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "TEMP", "TMP", "LANG", "LC_ALL", "TZ"}}
    result.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "roaming"),
        LOCALAPPDATA=str(home / "local"), CLAUDE_CONFIG_DIR=str(home / "config"),
        PI_CODING_AGENT_DIR=str(home / "config"), PI_OFFLINE="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1")
    return result


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""): digest.update(part)
    return digest.hexdigest()


def resolve(path=None, *, backend="claude", workspace=None):
    if backend not in REQUIRED: raise ValueError("Unsupported installed agent backend")
    workspace = Path(workspace or Path.cwd()).resolve()
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("External program must be an absolute installed path")
    else:
        found = shutil.which(backend)
        if not found:
            raise ValueError(backend + " is not installed; install it separately or choose native execution")
        candidate = Path(found)
        if os.name == "nt" and candidate.suffix.lower() in {".cmd", ".ps1"}:
            package = candidate.parent / {"claude":"node_modules/@anthropic-ai/claude-code/package.json", "pi":"node_modules/@earendil-works/pi-coding-agent/package.json", "codex":"node_modules/@openai/codex/package.json"}[backend]
            try:
                metadata = json.loads(package.read_text(encoding="utf-8"))
                binary = metadata["bin"][backend]
                expected = {"claude":"@anthropic-ai/claude-code", "pi":"@earendil-works/pi-coding-agent", "codex":"@openai/codex"}[backend]
                allowed = {"claude":{"bin/claude.exe", "cli.js"}, "pi":{"dist/bundle/cli.js"}, "codex":{"bin/codex.js"}}[backend]
                if metadata["name"] != expected or binary not in allowed:
                    raise ValueError("Unknown installed agent launcher")
                candidate = package.parent / binary
                if backend == "codex":
                    import platform
                    architecture = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64" if platform.machine().lower() in {"amd64", "x86_64"} else None
                    if architecture is None: raise ValueError("Unsupported installed Codex architecture")
                    triple = "aarch64-pc-windows-msvc" if architecture == "arm64" else "x86_64-pc-windows-msvc"
                    vendor = package.parent / ("node_modules/@openai/codex-win32-" + architecture) / "vendor" / triple
                    candidates = [vendor/"bin/codex.exe", vendor/"codex/codex.exe", package.parent/"vendor"/triple/"codex/codex.exe"]
                    candidate = next((p for p in candidates if p.is_file()), candidates[0])
            except (OSError, KeyError, ValueError) as error:
                raise ValueError("Select the actual installed agent program; shell shims are not executed") from error
    candidate = candidate.resolve(strict=True)
    if not path and candidate.is_relative_to(workspace):
        raise ValueError("An implicit agent program cannot come from the task workspace")
    if backend == "pi":
        try:
            package = candidate.parents[2]
            metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
            if metadata["name"] != "@earendil-works/pi-coding-agent" or metadata["bin"]["pi"] != "dist/bundle/cli.js" or candidate != (package / metadata["bin"]["pi"]).resolve():
                raise ValueError("Unknown Pi package entry")
            if not isinstance(metadata.get("version"), str) or not metadata["version"]:
                raise ValueError("Pi package version is missing")
            node = Path(shutil.which("node") or "").resolve(strict=True)
            if not node.is_file() or node.suffix.lower() not in {"", ".exe"} or node.is_relative_to(workspace):
                raise ValueError("Pi requires an installed Node executable outside the task workspace")
        except (OSError, KeyError, ValueError, IndexError) as error:
            raise ValueError("Select the installed Pi package's dist/bundle/cli.js and install Node >=22.19") from error
        return {"backend": backend, "program": str(candidate), "sha256": _hash(candidate), "runtime": str(node), "runtime_sha256": _hash(node),
            "package_sha256": _hash(package / "package.json"), "version": str(metadata["version"])}
    if candidate.suffix.lower() in {".cmd", ".bat", ".ps1", ".js"} or not candidate.is_file():
        raise ValueError("Select an installed executable, not a workspace command or shell script")
    return {"backend": backend, "program": str(candidate), "sha256": _hash(candidate)}


async def verify(binding, *, workspace=None):
    if resolve(binding["program"], backend=binding["backend"], workspace=workspace) != binding:
        raise ValueError("External program changed; inspect it before creating a new worker")
    cache = (binding["backend"], binding["sha256"], binding.get("runtime_sha256"), binding.get("package_sha256"))
    if cache in _probes:
        return _probes[cache]
    with tempfile.TemporaryDirectory(prefix="xuloom-program-probe-") as directory:
        args = [binding["program"], "--help"] if binding["backend"] in {"claude", "codex"} else [binding["runtime"], binding["program"], "--no-approve", "--no-extensions", "--no-context-files", "--offline", "--help"]
        process = await asyncio.create_subprocess_exec(*args, cwd=directory,
            env=environment(Path(directory)), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.kill(); await process.wait()  # Only this owned help probe.
    text = stdout.decode("utf-8", errors="replace")
    if process.returncode or not supports(text, REQUIRED[binding["backend"]]):
        raise ValueError("This installed agent lacks the required controlled streaming interface")
    _probes[cache] = {"controlled_tools": True, "stream_json": binding["backend"] == "claude", "rpc": binding["backend"] in {"pi", "codex"}}
    return _probes[cache]


def supports(help_text, flags):
    return all(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) for flag in flags)
