"""Execution locations are references to existing transports, never shell commands."""
import re


def executor(value=None):
    if value is None or value == "local":
        return "local"
    if not isinstance(value, str) or not re.fullmatch(r"ssh:[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("executor must be local or ssh:<configured-connection-id>")
    return value


def profile(value, context=None, frozen=None):
    from xuloom.remote.config import resolve, profile_key
    value = executor(value)
    if value == "local":
        return None
    sandbox = (context or {}).get("execution_sandbox")
    if sandbox is not None and sandbox.enabled:
        raise ValueError("SSH delegation is not available inside this system isolation backend; no host fallback was started")
    current = resolve(value[4:])
    if frozen is not None and profile_key(current) != profile_key(frozen):
        raise ValueError("Worker SSH target changed; restore its original target before continuing")
    return current
