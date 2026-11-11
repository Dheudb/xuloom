from xuloom.utils.shell import decode_output, process_options, terminate_process_tree
import asyncio
import json
import os
import copy
import sys
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional
from xuloom.branding import configuration_file

HOOK_EXECUTION_TIMEOUT_S = 120
_logger = logging.getLogger("xuloom.core.hooks")


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


@dataclass
class StopHookDecision:
    behavior: str
    reason: str = ""


@dataclass
class CommandHookResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    json_output: Optional[dict] = None


@dataclass
class BeforeToolUseDecision:
    behavior: str
    outputs: List[str]
    updated_input: Dict[str, Any]
    reason: str = ""
    history: Optional[List[Dict[str, Any]]] = None


@dataclass
class PostSamplingHookResult:
    outputs: List[Dict[str, Any]]


# Lifecycle events. Key: event name used in hooks.json and engine.add_hook().
# Value: (description, decisions the event honours besides observing/adding context).
HOOK_EVENTS = {
    "SessionStart": ("First run of a session, before the first model request.", ()),
    "UserPromptSubmit": ("A new user task is about to be sent; may add context or block it.", ("block",)),
    "BeforeToolUse": ("Before a tool executes; may reject the call or rewrite its input.", ("reject", "updated_input")),
    "AfterToolUse": ("After a tool result is saved; may add context for the next model request.", ()),
    "PostSampling": ("After the final answer of a turn is sampled (alias of PostSamplingHook).", ()),
    "Stop": ("Before a final answer is accepted; may block or request a retry (alias of StopHook).", ("block", "retry")),
    "TurnEnd": ("After a turn completed successfully.", ()),
    "TurnError": ("A turn ended with an error or was cancelled.", ()),
    "PreCompact": ("Before context compaction runs.", ()),
    "PostCompact": ("After context compaction changed the history.", ()),
    "SubagentStart": ("A worker/subagent record was created.", ()),
    "SubagentStop": ("A worker/subagent reached a terminal status.", ()),
    "SessionEnd": ("The engine is closing.", ()),
}
# Historical configuration keys kept as aliases.
HOOK_EVENT_ALIASES = {"StopHook": "Stop", "PostSamplingHook": "PostSampling"}


@dataclass
class HookOutcome:
    """Merged result of all hooks for one event."""
    decision: str = "allow"           # allow | block | retry | reject
    reason: str = ""
    context: List[str] = field(default_factory=list)
    updated_input: Optional[Dict[str, Any]] = None


@dataclass
class _Registration:
    event: str
    callback: Callable
    tools: Optional[frozenset] = None
    modes: Optional[frozenset] = None


class HookManager:
    """
    Lifecycle hooks with shell command execution support.

    Hooks are loaded from `.xuloom/hooks.json`. Each hook entry can be either:
      - Template hook (default): renders a text template with payload vars
      - Command hook (type: "command"): executes a shell command, feeds payload
        as JSON via stdin, and interprets stdout/exit-code as the hook response

    Command hooks enable self-correction: a StopHook can run linting/tests and,
    if the command exits with code 2 (or returns JSON with decision=block), the
    agent retries with the error feedback injected into context.
    """
    # Assigned by the owning engine so command hooks honor its execution isolation (declared only).
    execution_sandbox: Any

    def __init__(self, config_path=None, *, cwd=None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.config_path = os.path.abspath(os.path.join(self.cwd, os.fspath(config_path))) if config_path else str(configuration_file(self.cwd, "hooks.json"))
        self._callbacks: List[_Registration] = []
        self._reported: set = set()

    def _report(self, what: str, error: BaseException) -> None:
        """Log a hook configuration/evaluation failure once per distinct message."""
        message = f"{what}: {type(error).__name__}: {error}"
        if message not in self._reported:
            self._reported.add(message)
            _logger.warning("Hook %s", message)

    # ── Python callbacks ────────────────────────────────────────────────

    def register(self, event: str, callback: Callable, *, tools: Optional[Iterable[str]] = None,
                 modes: Optional[Iterable[str]] = None) -> _Registration:
        """Register a Python hook. ``callback(payload)`` may be sync or async.

        Return ``None`` to observe, or a dict with any of ``decision``
        (``"block"``, ``"retry"``, ``"reject"``), ``reason``, ``context`` (text
        added for the model) and, for BeforeToolUse, ``updated_input``.
        """
        event = HOOK_EVENT_ALIASES.get(event, event)
        if event not in HOOK_EVENTS:
            raise ValueError("Unknown hook event: " + str(event) + ". Known: " + ", ".join(HOOK_EVENTS))
        if not callable(callback):
            raise TypeError("Hook callback must be callable")
        registration = _Registration(event, callback, frozenset(tools) if tools else None, frozenset(modes) if modes else None)
        self._callbacks.append(registration)
        return registration

    def unregister(self, registration: _Registration) -> None:
        self._callbacks = [item for item in self._callbacks if item is not registration]

    def has_callbacks(self, event: str) -> bool:
        return any(item.event == event for item in self._callbacks)

    def has_configured(self, event: str) -> bool:
        try:
            return bool(self._load_config().get(event))
        except Exception as error:
            # Unreadable hooks.json: let the dispatcher run so the failure is reported (and,
            # for BeforeToolUse, the tool call is rejected) instead of silently skipping hooks.
            self._report(f"configuration {self.config_path} is unreadable", error)
            return True

    async def run_callbacks(self, event: str, payload: Dict[str, Any], *, fail_closed: bool = False) -> HookOutcome:
        """Run Python callbacks for ``event`` in registration order and merge their answers."""
        outcome = HookOutcome()
        allowed = set(HOOK_EVENTS[event][1])
        current_input = copy.deepcopy(payload.get("tool_input")) if "updated_input" in allowed else None
        for registration in list(self._callbacks):
            if registration.event != event:
                continue
            if registration.tools and str(payload.get("tool_name", "")) not in registration.tools:
                continue
            if registration.modes and str(payload.get("mode", "")) not in registration.modes:
                continue
            view = copy.deepcopy(dict(payload))
            if current_input is not None:
                view["tool_input"] = copy.deepcopy(current_input)
            try:
                answer = registration.callback(view)
                if inspect.isawaitable(answer):
                    answer = await answer
            except Exception as error:
                _logger.warning("Hook callback for %s failed: %s", event, type(error).__name__)
                if fail_closed:
                    return HookOutcome("reject", f"{event} hook failed ({type(error).__name__}); the action was not run.",
                                       outcome.context, current_input)
                continue
            if answer is None:
                continue
            if not isinstance(answer, dict):
                _logger.warning("Hook callback for %s returned %s; expected dict or None", event, type(answer).__name__)
                continue
            text = answer.get("context")
            if text:
                outcome.context.append(str(text))
            if "updated_input" in allowed and isinstance(answer.get("updated_input"), dict):
                current_input = copy.deepcopy(answer["updated_input"])
                outcome.updated_input = current_input
            decision = str(answer.get("decision", "allow") or "allow").strip().lower()
            decision = {"deny": "reject", "approve": "allow", "continue": "allow"}.get(decision, decision)
            if decision in allowed and decision != "updated_input":
                if outcome.decision == "allow" or decision in {"block", "reject"}:
                    outcome.decision, outcome.reason = decision, str(answer.get("reason", "") or "")
                if decision in {"block", "reject"}:
                    break
        return outcome

    async def dispatch(self, event: str, payload: Dict[str, Any]) -> HookOutcome:
        """Run configured (template/command) hooks and Python callbacks for a generic event."""
        outcome = HookOutcome()
        allowed = set(HOOK_EVENTS[event][1])
        try:
            config = self._load_config()
        except Exception as error:
            self._report(f"configuration {self.config_path} is unreadable; {event} hooks were skipped", error)
            config = {}
        for hook in config.get(event, []):
            try:
                if not self._hook_matches(hook, payload):
                    continue
                if str(hook.get("type", "template")).strip().lower() == "command":
                    result = await self._exec_command_hook(hook, payload)
                    data = result.json_output or {}
                    text = data.get("context") or data.get("additionalContext")
                    if text:
                        outcome.context.append(str(text))
                    decision = str(data.get("decision", "") or "").lower()
                    if result.exit_code == 2 and not decision:
                        decision = "block"
                    if decision in allowed and outcome.decision == "allow":
                        outcome.decision = decision
                        outcome.reason = str(data.get("reason", "") or result.stderr.strip() or result.stdout.strip())
                else:
                    template = str(hook.get("content", "")).strip()
                    rendered = self._render_template(template, payload) if template else ""
                    behavior = str(hook.get("behavior", "observe") or "observe").strip().lower()
                    if behavior in allowed and outcome.decision == "allow":
                        outcome.decision, outcome.reason = behavior, rendered
                    elif rendered:
                        outcome.context.append(rendered)
            except Exception as error:
                self._report(f"{event} hook from {self.config_path} failed and was skipped", error)
                continue
        callbacks = await self.run_callbacks(event, payload)
        outcome.context.extend(callbacks.context)
        if outcome.decision == "allow" and callbacks.decision != "allow":
            outcome.decision, outcome.reason = callbacks.decision, callbacks.reason
        return outcome

    def execute(self, event: str, payload: Dict[str, Any]) -> List[str]:
        try:
            config = self._load_config()
        except Exception as error:
            self._report(f"configuration {self.config_path} is unreadable; {event} hooks were skipped", error)
            return []

        rendered_outputs = []
        for hook in config.get(event, []):
            try:
                if not self._hook_matches(hook, payload):
                    continue

                template = str(hook.get("content", "")).strip()
                if not template:
                    continue

                rendered = template.format_map(_SafeFormatDict(self._stringify_payload(payload))).strip()
                if rendered:
                    rendered_outputs.append(rendered)
            except Exception as error:
                self._report(f"{event} hook from {self.config_path} failed and was skipped", error)
                continue

        return rendered_outputs

    async def evaluate_stop_hooks_async(self, payload: Dict[str, Any]) -> StopHookDecision:
        """Evaluate StopHooks, supporting both template and command hooks."""
        try:
            config = self._load_config()
        except Exception as error:
            self._report(f"configuration {self.config_path} is unreadable; Stop hooks were skipped", error)
            return StopHookDecision("allow", "")

        blocking_reasons = []
        retry_reasons = []

        for hook in config.get("StopHook", []):
            try:
                if not self._hook_matches(hook, payload):
                    continue

                hook_type = str(hook.get("type", "template")).strip().lower()

                if hook_type == "command":
                    result = await self._exec_command_hook(hook, payload)
                    decision = self._interpret_command_result(result, hook)
                    if decision.behavior == "block":
                        blocking_reasons.append(decision.reason or "Blocked by command hook.")
                    elif decision.behavior == "retry":
                        retry_reasons.append(decision.reason or "Retry requested by command hook.")
                else:
                    template = str(hook.get("content", "")).strip()
                    rendered = ""
                    if template:
                        rendered = template.format_map(_SafeFormatDict(self._stringify_payload(payload))).strip()

                    behavior = str(
                        hook.get("behavior", hook.get("action", "allow"))
                    ).strip().lower()

                    if behavior == "block":
                        blocking_reasons.append(rendered or "Blocked by stop hook.")
                    elif behavior == "retry":
                        retry_reasons.append(rendered or "Retry requested by stop hook.")
            except Exception as error:
                self._report(f"Stop hook from {self.config_path} failed and was skipped", error)
                continue

        if blocking_reasons:
            return StopHookDecision("block", "\n".join(blocking_reasons))
        if retry_reasons:
            return StopHookDecision("retry", "\n".join(retry_reasons))
        return StopHookDecision("allow", "")

    def evaluate_stop_hooks(self, payload: Dict[str, Any]) -> StopHookDecision:
        """Sync fallback — only evaluates template hooks (no command execution)."""
        try:
            config = self._load_config()
        except Exception as error:
            self._report(f"configuration {self.config_path} is unreadable; Stop hooks were skipped", error)
            return StopHookDecision("allow", "")

        blocking_reasons = []
        retry_reasons = []

        for hook in config.get("StopHook", []):
            try:
                if not self._hook_matches(hook, payload):
                    continue

                if str(hook.get("type", "template")).strip().lower() == "command":
                    continue

                template = str(hook.get("content", "")).strip()
                rendered = ""
                if template:
                    rendered = template.format_map(_SafeFormatDict(self._stringify_payload(payload))).strip()

                behavior = str(
                    hook.get("behavior", hook.get("action", "allow"))
                ).strip().lower()

                if behavior == "block":
                    blocking_reasons.append(rendered or "Blocked by stop hook.")
                elif behavior == "retry":
                    retry_reasons.append(rendered or "Retry requested by stop hook.")
            except Exception as error:
                self._report(f"Stop hook from {self.config_path} failed and was skipped", error)
                continue

        if blocking_reasons:
            return StopHookDecision("block", "\n".join(blocking_reasons))
        if retry_reasons:
            return StopHookDecision("retry", "\n".join(retry_reasons))
        return StopHookDecision("allow", "")

    def evaluate_before_tool_hooks(self, payload: Dict[str, Any]) -> BeforeToolUseDecision:
        try:
            config = self._load_config()
        except Exception as error:
            # Fail closed: an unreadable hooks.json may contain rules that reject this call.
            self._report(f"configuration {self.config_path} is unreadable; tool calls are rejected until it is fixed", error)
            return BeforeToolUseDecision(
                behavior="reject",
                outputs=[],
                updated_input=copy.deepcopy(payload.get("tool_input", {}) or {}),
                reason=(f"Hook configuration {self.config_path} could not be read ({type(error).__name__}); "
                        "BeforeToolUse rules cannot be checked, so the tool was not run. Fix or remove the file."),
                history=[],
            )

        current_input = copy.deepcopy(payload.get("tool_input", {}) or {})
        rendered_outputs = []
        history = []

        for index, hook in enumerate(config.get("BeforeToolUse", []), start=1):
            try:
                effective_payload = dict(payload)
                effective_payload["tool_input"] = current_input
                if not self._hook_matches(hook, effective_payload):
                    continue

                template = str(hook.get("content", "")).strip()
                rendered = ""
                if template:
                    rendered = self._render_template(template, effective_payload)
                    if rendered:
                        rendered_outputs.append(rendered)

                previous_input = copy.deepcopy(current_input)
                if isinstance(hook.get("updated_input"), dict):
                    current_input = self._render_structured_value(
                        hook.get("updated_input"),
                        effective_payload,
                    )
                elif isinstance(hook.get("input_patch"), dict):
                    rendered_patch = self._render_structured_value(
                        hook.get("input_patch"),
                        effective_payload,
                    )
                    if isinstance(rendered_patch, dict):
                        current_input = {
                            **current_input,
                            **rendered_patch,
                        }

                behavior = str(
                    hook.get("behavior", hook.get("action", "allow"))
                ).strip().lower()
                normalized_behavior = {
                    "approve": "allow",
                    "accept": "allow",
                    "allow": "allow",
                    "continue": "allow",
                    "deny": "reject",
                    "block": "reject",
                    "reject": "reject",
                }.get(behavior, "allow")

                input_changed = previous_input != current_input
                history.append({
                    "hook_index": index,
                    "behavior": normalized_behavior,
                    "tool_name": payload.get("tool_name", ""),
                    "tool_use_id": payload.get("tool_use_id", ""),
                    "rendered": rendered,
                    "input_changed": input_changed,
                    "updated_input": copy.deepcopy(current_input) if input_changed else None,
                })

                if normalized_behavior == "reject":
                    return BeforeToolUseDecision(
                        behavior="reject",
                        outputs=rendered_outputs,
                        updated_input=current_input,
                        reason=rendered or "Rejected by BeforeToolUse hook.",
                        history=history,
                    )
            except Exception as error:
                # Fail closed, like Python BeforeToolUse callbacks: a broken rule must not let the call through.
                self._report(f"BeforeToolUse hook #{index} from {self.config_path} failed", error)
                return BeforeToolUseDecision(
                    behavior="reject",
                    outputs=rendered_outputs,
                    updated_input=current_input,
                    reason=f"BeforeToolUse hook #{index} failed ({type(error).__name__}); the tool was not run.",
                    history=history,
                )

        return BeforeToolUseDecision(
            behavior="allow",
            outputs=rendered_outputs,
            updated_input=current_input,
            history=history,
        )

    def evaluate_post_sampling_hooks(self, payload: Dict[str, Any]) -> PostSamplingHookResult:
        try:
            config = self._load_config()
        except Exception as error:
            self._report(f"configuration {self.config_path} is unreadable; PostSampling hooks were skipped", error)
            return PostSamplingHookResult(outputs=[])

        outputs = []
        for index, hook in enumerate(config.get("PostSamplingHook", []), start=1):
            try:
                if not self._hook_matches(hook, payload):
                    continue

                template = str(hook.get("content", "")).strip()
                rendered = ""
                if template:
                    rendered = self._render_template(template, payload)

                if not rendered:
                    continue

                outputs.append({
                    "hook_index": index,
                    "behavior": str(hook.get("behavior", "observe") or "observe").strip().lower(),
                    "severity": str(hook.get("severity", "info") or "info").strip().lower(),
                    "rendered": rendered,
                })
            except Exception as error:
                self._report(f"PostSampling hook #{index} from {self.config_path} failed and was skipped", error)
                continue

        return PostSamplingHookResult(outputs=outputs)

    # ── Command hook execution ──────────────────────────────────────────

    async def _exec_command_hook(
        self,
        hook: Dict[str, Any],
        payload: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> CommandHookResult:
        """
        Execute a command-type hook via subprocess.

        The hook payload is serialised as JSON and piped to stdin.
        stdout/stderr are captured; the exit code drives the hook decision:
          0  → allow (success)
          2  → block (the agent must retry with the stderr/stdout as feedback)
          other non-zero → non-blocking error (logged, but doesn't block)
        If stdout is valid JSON, it is parsed for structured responses
        (keys: "decision", "reason", "continue").
        """
        command = str(hook.get("command", "")).strip()
        if not command:
            return CommandHookResult(stderr="No command specified in hook.", exit_code=1)
        if getattr(getattr(self, "execution_sandbox", None), "enabled", False):
            return CommandHookResult(stderr="Command hooks are disabled in system workspace isolation; no host execution occurred.", exit_code=2)

        timeout = timeout or hook.get("timeout") or HOOK_EXECUTION_TIMEOUT_S
        json_input = json.dumps(payload, ensure_ascii=False, default=str)

        env = os.environ.copy()
        env["XULOOM_PROJECT_DIR"] = self.cwd

        is_windows = sys.platform == "win32"
        shell_cmd = command
        proc = None

        try:
            if is_windows:
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=self.cwd,
                    **process_options(),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/sh", "-c", shell_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=self.cwd,
                    **process_options(),
                )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=(json_input + "\n").encode("utf-8")),
                timeout=timeout,
            )

            stdout = decode_output(stdout_bytes) if stdout_bytes else ""
            stderr = decode_output(stderr_bytes) if stderr_bytes else ""
            exit_code = proc.returncode or 0

            json_output = None
            if stdout.strip():
                try:
                    json_output = json.loads(stdout.strip().split("\n")[0])
                    if not isinstance(json_output, dict):
                        json_output = None
                except (json.JSONDecodeError, IndexError):
                    json_output = None

            return CommandHookResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                json_output=json_output,
            )

        except asyncio.CancelledError:
            await asyncio.shield(terminate_process_tree(proc))
            raise
        except asyncio.TimeoutError:
            await terminate_process_tree(proc)
            return CommandHookResult(
                stderr=f"Hook command timed out after {timeout}s: {command}",
                exit_code=-1,
                timed_out=True,
            )
        except Exception as e:
            return CommandHookResult(
                stderr=f"Hook command failed to execute: {e}",
                exit_code=-1,
            )

    def _interpret_command_result(
        self,
        result: CommandHookResult,
        hook: Dict[str, Any],
    ) -> StopHookDecision:
        """
        Interpret a CommandHookResult into a StopHookDecision.

        Priority: JSON output > exit code > hook config behavior.
        """
        if result.json_output and isinstance(result.json_output, dict):
            jout = result.json_output
            decision = str(jout.get("decision", "")).strip().lower()
            reason = str(jout.get("reason", "")).strip()

            if jout.get("continue") is False:
                return StopHookDecision("allow", reason or "Hook stopped continuation.")

            if decision == "block":
                feedback = reason or result.stderr.strip() or result.stdout.strip() or "Blocked by command hook."
                return StopHookDecision("block", feedback)
            elif decision in ("retry", "reject"):
                feedback = reason or result.stderr.strip() or result.stdout.strip() or "Retry requested by command hook."
                return StopHookDecision("retry", feedback)
            elif decision in ("allow", "approve", "accept"):
                return StopHookDecision("allow", "")

        if result.exit_code == 2:
            feedback = (
                result.stderr.strip()
                or result.stdout.strip()
                or "Command hook exited with blocking status (exit code 2)."
            )
            return StopHookDecision("block", feedback)

        if result.exit_code != 0 and result.exit_code != -1:
            return StopHookDecision("allow", "")

        if result.timed_out:
            return StopHookDecision("allow", "")

        return StopHookDecision("allow", "")

    # ── Shared utilities ────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, list]:
        if not os.path.exists(self.config_path):
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return {}
        for legacy, current in HOOK_EVENT_ALIASES.items():
            # Both spellings are accepted; legacy keys run first.
            merged = list(payload.get(legacy, []) or []) + list(payload.get(current, []) or [])
            payload[legacy] = payload[current] = merged
        return payload

    def _hook_matches(self, hook: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        tools = hook.get("tools")
        if tools:
            tool_name = str(payload.get("tool_name", "")).strip()
            if tool_name not in set(str(item) for item in tools):
                return False

        modes = hook.get("modes")
        if modes:
            current_mode = str(payload.get("mode", "")).strip()
            if current_mode not in set(str(item) for item in modes):
                return False

        return True

    def _stringify_payload(self, payload: Dict[str, Any]) -> Dict[str, str]:
        rendered = {}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                rendered[key] = json.dumps(value, ensure_ascii=False)
            else:
                rendered[key] = str(value)
        return rendered

    def _render_template(self, template: str, payload: Dict[str, Any]) -> str:
        return template.format_map(_SafeFormatDict(self._stringify_payload(payload))).strip()

    def _render_structured_value(self, value, payload: Dict[str, Any]):
        if isinstance(value, dict):
            return {
                key: self._render_structured_value(inner, payload)
                for key, inner in value.items()
            }
        if isinstance(value, list):
            return [self._render_structured_value(item, payload) for item in value]
        if isinstance(value, str):
            return self._render_template(value, payload)
        return copy.deepcopy(value)
