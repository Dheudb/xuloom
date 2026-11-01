"""Resolve an installed program without shell interpolation or downloads."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

REQUIRED = ("--restricted", "--bare", "--tools", "--strict-mcp-config", "--input-format",
            "--output-format", "--include-partial-messages", "--system-prompt", "--session-id")
_probes = {}


def environment(home):
    # The selected provider key lives in the Python relay, never this process.
    result = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "TEMP", "TMP", "LANG", "LC_ALL", "TZ"}}
    result.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "roaming"),
        LOCALAPPDATA=str(home / "local"), CLAUDE_CONFIG_DIR=str(home / "config"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1")
    return result


def resolve(path=None):
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("External program must be an absolute installed path")
    else:
        found = shutil.which("claude")
        if not found:
            raise ValueError("Claude Code is not installed; install it separately or choose native execution")
        candidate = Path(found)
        if os.name == "nt" and candidate.suffix.lower() in {".cmd", ".ps1"}:
            package = candidate.parent / "node_modules/@anthropic-ai/claude-code/package.json"
            try:
                metadata = json.loads(package.read_text(encoding="utf-8"))
                binary = metadata["bin"]["claude"]
                if metadata["name"] != "@anthropic-ai/claude-code" or binary not in {"bin/claude.exe", "cli.js"}:
                    raise ValueError("Unknown Claude Code launcher")
                candidate = package.parent / binary
            except (OSError, KeyError, ValueError) as error:
                raise ValueError("Select the actual installed Claude Code executable; shell shims are not executed") from error
    candidate = candidate.resolve(strict=True)
    if (not path and candidate.parent == Path.cwd().resolve()) or candidate.suffix.lower() in {".cmd", ".bat", ".ps1", ".js"} or not candidate.is_file():
        raise ValueError("Select an installed executable, not a workspace command or shell script")
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""): digest.update(part)
    return {"backend": "claude", "program": str(candidate), "sha256": digest.hexdigest()}


async def verify(binding):
    if resolve(binding["program"]) != binding:
        raise ValueError("External program changed; inspect it before creating a new worker")
    cache = binding["sha256"]
    if cache in _probes:
        return _probes[cache]
    with tempfile.TemporaryDirectory(prefix="xuloom-program-probe-") as directory:
        process = await asyncio.create_subprocess_exec(binding["program"], "--help", cwd=directory,
            env=environment(Path(directory)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.kill(); await process.wait()  # Only this owned help probe.
    text = stdout.decode("utf-8", errors="replace")
    if process.returncode or any(flag not in text for flag in REQUIRED):
        raise ValueError("This Claude Code installation lacks the required controlled streaming interface")
    _probes[cache] = {"controlled_tools": True, "stream_json": True}
    return _probes[cache]
