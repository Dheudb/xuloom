"""Live parent-side decisions for an authenticated delegated execution channel.

No policy files or credentials are shipped to the child. The transport supplies
the real child tool request; local restrictive ancestry is evaluated here on
each call, without executing or reinterpreting a remote tool on this machine.
"""
import copy
from dataclasses import fields, replace
import inspect
import re
from types import SimpleNamespace

from xuloom.security.permissions import PermissionConfigError, PermissionDecision, PermissionManager, PermissionRequest


def request_from_wire(value):
    expected = {field.name for field in fields(PermissionRequest)}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Invalid delegated permission fields")
    if not isinstance(value["tool_name"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value["tool_name"]):
        raise ValueError("Invalid delegated tool identity")
    if any(not isinstance(value[key], str) for key in ("summary", "risk_level", "mode", "tool_description")):
        raise ValueError("Invalid delegated permission text")
    if type(value["is_read_only"]) is not bool or type(value["denial_count"]) is not int or value["denial_count"] < 0:
        raise ValueError("Invalid delegated permission attributes")
    if not isinstance(value["input_payload"], dict) or not isinstance(value["classifier_input"], dict):
        raise ValueError("Invalid delegated permission inputs")
    reasons = value["classifier_input"].get("forced_prompt_reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise ValueError("Invalid delegated approval requirement")
    return PermissionRequest(**copy.deepcopy(value))


async def authorize_delegated(manager, request, automatic=None, aliases=None, *, remote=None, grant_revision=None):
    """Return a decision and child-only grant instruction, never a parent allow memory.

    `request`, `automatic` and `aliases` come from the authenticated child
    framework, not model arguments. Each transport must validate and correlate
    its worker/channel identity before invoking this function.
    """
    if not isinstance(manager, PermissionManager) or not isinstance(request, PermissionRequest):
        raise ValueError("A live parent permission manager and child request are required")
    aliases = aliases or {}
    if not isinstance(aliases, dict) or any(not isinstance(name, str) or not isinstance(defaults, dict) for name, defaults in aliases.items()):
        raise ValueError("Invalid delegated permission aliases")
    tool = SimpleNamespace(permission_aliases=copy.deepcopy(aliases))
    request = copy.deepcopy(request)
    if remote is not None:
        request.classifier_input["remote"] = copy.deepcopy(remote)
    forced = list(request.classifier_input.get("forced_prompt_reasons", []))
    seen, parent = set(), manager
    revision = manager.approval_policy.revision
    def answer(behavior, reason, *, remember=None):
        return {"behavior": behavior, "reason": reason, "remember": remember, "approval_revision": revision}
    while parent is not None:
        if not isinstance(parent, PermissionManager) or id(parent) in seen:
            return answer("deny", "Invalid delegating permission ancestry")
        seen.add(id(parent))
        mode = parent.mode_getter() if callable(parent.mode_getter) else "normal"
        inherited = replace(request, mode=mode)
        # A plan path belongs to a machine and session. A remote file with the
        # same spelling as the local plan is never an authorized plan write.
        if mode == "plan" and request.tool_name in {"file_write_tool", "file_edit_tool"}:
            return answer("deny", "The delegating session is in Plan mode; remote file changes are not authorized")
        plan = parent._plan_decision(inherited)
        if plan and plan.behavior == "deny":
            return answer("deny", "Delegating session: " + plan.reason)
        try:
            config = parent._load_config()
        except PermissionConfigError:
            return answer("deny", "Delegating permission configuration is invalid or unreadable")
        for candidate in parent._rule_requests(tool, inherited):
            decision = parent._evaluate_rule_layer(candidate, config)
            if decision and decision.behavior == "deny":
                return answer("deny", "Delegating session: " + decision.reason)
            if decision and decision.behavior == "ask":
                forced.append("Delegating session: " + decision.reason)
            if parent._should_escalate_denial(candidate.tool_name):
                return answer("deny", "Delegating session's repeated-denial safeguard applies")
        if parent.approval_policy.mode != "full":
            if parent is not manager and not request.is_read_only:
                forced.append("A delegating ancestor requires approval")
            classified = parent._run_auto_classifier(inherited)
            if classified and classified.behavior == "deny":
                return answer("deny", classified.reason)
            if classified and classified.behavior == "ask":
                forced.append(classified.reason or "Delegating classifier requires approval")
        parent = parent.restriction_parent
    child_allow = isinstance(automatic, PermissionDecision) and automatic.behavior == "allow" and automatic.source not in {"full_access", "read_only"}
    # A child session grant is invalid after the parent's approval mode changes.
    if child_allow and automatic.source == "session_rule" and grant_revision != revision:
        child_allow = False
    if not forced and (manager.approval_policy.mode == "full" or request.is_read_only or child_allow):
        return answer("allow", "Delegating session policy permits this call")
    if manager.prompt_handler is None:
        return answer("deny", "Delegating session requires approval but has no interactive approval handler")
    label = " · ".join(str((remote or {}).get(key) or "") for key in ("host", "cwd")).strip(" ·")
    if label:
        request.summary = "Remote worker: " + label + "\n" + request.summary
    if forced:
        request.summary += "\nPermission policy notes:\n- " + "\n- ".join(dict.fromkeys(forced))
        request.classifier_input["forced_prompt_reasons"] = forced
    raw = manager.prompt_handler(request)
    if inspect.isawaitable(raw):
        raw = await raw
    raw = str(raw or "").strip().lower()
    if raw in {"allow", "y", "yes", "always", "a"}:
        return answer("allow", "Approved for this delegated call", remember="allow" if raw in {"always", "a"} else None)
    return answer("deny", "Denied by the delegating user", remember="deny" if raw in {"never", "deny_always", "block_always"} else None)
