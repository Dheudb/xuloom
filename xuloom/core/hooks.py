from xuloom.utils.shell import decode_output, process_options, terminate_process_tree
import asyncio
import json
import os
import copy
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional
from xuloom.branding import configuration_file

HOOK_EXECUTION_TIMEOUT_S = 120


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
    updated_input: Dict[str, object]
    reason: str = ""
    history: List[Dict[str, object]] = None


@dataclass
class PostSamplingHookResult:
    outputs: List[Dict[str, object]]


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

    def __init__(self, config_path=None, *, cwd=None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.config_path = os.path.abspath(os.path.join(self.cwd, os.fspath(config_path))) if config_path else str(configuration_file(self.cwd, "hooks.json"))

    def execute(self, event: str, payload: Dict[str, object]) -> List[str]:
        try:
            config = self._load_config()
        except Exception:
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
            except Exception:
                continue

        return rendered_outputs

    async def evaluate_stop_hooks_async(self, payload: Dict[str, object]) -> StopHookDecision:
        """Evaluate StopHooks, supporting both template and command hooks."""
        try:
            config = self._load_config()
        except Exception:
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
            except Exception:
                continue

        if blocking_reasons:
            return StopHookDecision("block", "\n".join(blocking_reasons))
        if retry_reasons:
            return StopHookDecision("retry", "\n".join(retry_reasons))
        return StopHookDecision("allow", "")

    def evaluate_stop_hooks(self, payload: Dict[str, object]) -> StopHookDecision:
        """Sync fallback — only evaluates template hooks (no command execution)."""
        try:
            config = self._load_config()
        except Exception:
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
            except Exception:
                continue

        if blocking_reasons:
            return StopHookDecision("block", "\n".join(blocking_reasons))
        if retry_reasons:
            return StopHookDecision("retry", "\n".join(retry_reasons))
        return StopHookDecision("allow", "")

    def evaluate_before_tool_hooks(self, payload: Dict[str, object]) -> BeforeToolUseDecision:
        try:
            config = self._load_config()
        except Exception:
            return BeforeToolUseDecision(
                behavior="allow",
                outputs=[],
                updated_input=copy.deepcopy(payload.get("tool_input", {}) or {}),
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
            except Exception:
                continue

        return BeforeToolUseDecision(
            behavior="allow",
            outputs=rendered_outputs,
            updated_input=current_input,
            history=history,
        )

    def evaluate_post_sampling_hooks(self, payload: Dict[str, object]) -> PostSamplingHookResult:
        try:
            config = self._load_config()
        except Exception:
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
            except Exception:
                continue

        return PostSamplingHookResult(outputs=outputs)

    # ── Command hook execution ──────────────────────────────────────────

    async def _exec_command_hook(
        self,
        hook: Dict[str, object],
        payload: Dict[str, object],
        timeout: float = None,
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
        hook: Dict[str, object],
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
        return payload

    def _hook_matches(self, hook: Dict[str, object], payload: Dict[str, object]) -> bool:
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

    def _stringify_payload(self, payload: Dict[str, object]) -> Dict[str, str]:
        rendered = {}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                rendered[key] = json.dumps(value, ensure_ascii=False)
            else:
                rendered[key] = str(value)
        return rendered

    def _render_template(self, template: str, payload: Dict[str, object]) -> str:
        return template.format_map(_SafeFormatDict(self._stringify_payload(payload))).strip()

    def _render_structured_value(self, value, payload: Dict[str, object]):
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
