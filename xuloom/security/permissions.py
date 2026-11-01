import copy
import fnmatch
import inspect
import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional

from xuloom.features.plans import is_session_plan_file
from xuloom.branding import configuration_file


class PermissionConfigError(ValueError):
    """An existing policy cannot safely be interpreted as an empty policy."""


@dataclass
class ApprovalPolicy:
    mode: str = "ask"
    revision: int = 0

    def __post_init__(self):
        if self.mode not in {"ask", "full"}:
            raise ValueError("权限模式必须是 ask 或 full")

    def set_mode(self, mode):
        if mode not in {"ask", "full"}:
            raise ValueError("权限模式必须是 ask 或 full")
        if mode != self.mode:
            self.mode = mode
            self.revision += 1


@dataclass
class PermissionRequest:
    tool_name: str
    summary: str
    risk_level: str
    input_payload: Dict[str, Any]
    mode: str
    is_read_only: bool
    tool_description: str
    classifier_input: Dict[str, Any]
    denial_count: int = 0


@dataclass
class PermissionDecision:
    behavior: str
    reason: str = ""
    source: str = "unknown"
    metadata: Dict[str, Any] = None


class PermissionManager:
    """
    Session-scoped permission gate for tool calls.

    Read-only tools are auto-approved. Mutating or high-risk tools must pass
    through an allow/deny/ask flow, with optional session-wide allow rules.
    """

    def __init__(
        self,
        prompt_handler: Optional[Callable[[PermissionRequest], Any]] = None,
        mode_getter: Optional[Callable[[], str]] = None,
        state_change_callback: Optional[Callable[[], Any]] = None,
        classifier_manager=None,
        config_path: Optional[str] = None,
        session_id_getter: Optional[Callable[[], str]] = None,
        approval_policy: Optional[ApprovalPolicy] = None,
    ):
        self.prompt_handler = prompt_handler
        self.mode_getter = mode_getter
        self.state_change_callback = state_change_callback
        self.classifier_manager = classifier_manager
        self.config_path = str(config_path or configuration_file(os.getcwd(), "permissions.json"))
        self.session_id_getter = session_id_getter
        self.approval_policy = approval_policy or ApprovalPolicy()
        self._approval_revision = self.approval_policy.revision
        self.always_allow_tools = set()
        self.always_deny_tools = set()
        self.always_ask_tools = set()
        self.plan_mode_allowlist = {
            "todo_write_tool", "plan_tool", "exit_plan_mode",
            "file_read_tool", "grep_tool", "glob_tool",
            "tool_search_tool", "web_search_tool", "web_fetch_tool",
            "agent_tool", "ask_user_question_tool", "lsp_tool",
            "skill_tool",
            "file_write_tool", "file_edit_tool",
        }
        self.denial_history = []
        self.consecutive_denials = {}
        self.denial_escalation_threshold = 3
        self.pending_request = None
        # Live restrictive ancestry, never an inherited approval memory.
        self.restriction_parent = None

    def set_prompt_handler(self, prompt_handler: Optional[Callable[[PermissionRequest], Any]]):
        self.prompt_handler = prompt_handler

    def set_mode_getter(self, mode_getter: Optional[Callable[[], str]]):
        self.mode_getter = mode_getter

    def set_state_change_callback(self, callback: Optional[Callable[[], Any]]):
        self.state_change_callback = callback

    def set_classifier_manager(self, classifier_manager):
        self.classifier_manager = classifier_manager

    def export_state(self) -> Dict[str, Any]:
        return {
            "always_allow_tools": sorted(self.always_allow_tools),
            "always_deny_tools": sorted(self.always_deny_tools),
            "always_ask_tools": sorted(self.always_ask_tools),
            "denial_history": copy.deepcopy(self.denial_history),
            "consecutive_denials": dict(self.consecutive_denials),
            "pending_request": copy.deepcopy(self.pending_request),
        }

    def load_state(self, payload: Dict[str, Any]):
        payload = payload or {}
        self.always_allow_tools = set(payload.get("always_allow_tools", []) or [])
        self.always_deny_tools = set(payload.get("always_deny_tools", []) or [])
        self.always_ask_tools = set(payload.get("always_ask_tools", []) or [])
        self.denial_history = list(payload.get("denial_history", []) or [])[:20]
        self.consecutive_denials = {
            str(key): int(value)
            for key, value in dict(payload.get("consecutive_denials", {}) or {}).items()
        }
        pending_request = payload.get("pending_request")
        self.pending_request = copy.deepcopy(pending_request) if isinstance(pending_request, dict) else None

    async def authorize(
        self,
        tool: Any,
        input_payload: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        if self._approval_revision != self.approval_policy.revision:
            self.always_allow_tools.clear()
            self._approval_revision = self.approval_policy.revision
        tool_name = getattr(tool, "name", tool.__class__.__name__)
        current_mode = self.mode_getter() if callable(self.mode_getter) else "normal"
        risk_level = self._get_risk_level(tool, input_payload)
        is_read_only = self._is_read_only(tool, input_payload)

        request = PermissionRequest(
            tool_name=tool_name,
            summary=self._build_summary(tool, input_payload),
            risk_level=risk_level,
            input_payload=copy.deepcopy(input_payload),
            mode=current_mode,
            is_read_only=is_read_only,
            tool_description=str(getattr(tool, "description", "") or ""),
            classifier_input=self._build_classifier_input(
                tool_name=tool_name,
                input_payload=input_payload,
                mode=current_mode,
                risk_level=risk_level,
                is_read_only=is_read_only,
                summary=self._build_summary(tool, input_payload),
                context=context or {},
            ),
            denial_count=int(self.consecutive_denials.get(tool_name, 0) or 0),
        )

        plan_decision = self._plan_decision(request)
        if plan_decision and plan_decision.behavior == "deny":
            return self._finalize_permission_decision(tool_name, plan_decision, request)

        forced_prompt_reasons = (["An interrupted operation has uncertain effects. Inspect existing state before approving a new mutation."]
            if (context or {}).get("recovery_uncertain") and not is_read_only else [])

        try:
            config = self._load_config()
        except PermissionConfigError:
            return self._finalize_permission_decision(tool_name, PermissionDecision("deny",
                "权限配置无法读取或格式无效，已阻止执行。请修复配置后重试：" + str(self.config_path),
                source="config_error", metadata={"code": "invalid_permission_config"}), request)
        rule_decisions = [self._evaluate_rule_layer(candidate, config) for candidate in self._rule_requests(tool, request)]
        rule_decisions.extend(self._inherited_restrictions(tool, request))
        rule_decision = min((decision for decision in rule_decisions if decision),
            key=lambda decision: {"deny": 0, "ask": 1, "allow": 2}[decision.behavior], default=None)
        if rule_decision and rule_decision.behavior != "allow":
            finalized_rule_decision = self._finalize_permission_decision(tool_name, rule_decision, request)
            if finalized_rule_decision.behavior == "ask":
                forced_prompt_reasons.append(finalized_rule_decision.reason or "Permission rule requires approval.")
            else:
                return finalized_rule_decision

        tool_check_decision = await self._run_tool_permission_check(tool, request)
        if tool_check_decision and tool_check_decision.behavior != "allow":
            finalized_tool_check = self._finalize_permission_decision(tool_name, tool_check_decision, request)
            if finalized_tool_check.behavior == "ask":
                forced_prompt_reasons.append(finalized_tool_check.reason or "Tool-specific permission check requires approval.")
            else:
                return finalized_tool_check

        classifier_decision = self._run_auto_classifier(request) if self.approval_policy.mode != "full" else None
        if classifier_decision and classifier_decision.behavior != "allow":
            finalized_classifier = self._finalize_permission_decision(tool_name, classifier_decision, request)
            if finalized_classifier.behavior == "ask":
                forced_prompt_reasons.append(finalized_classifier.reason or "Automatic security classifier requested approval.")
            else:
                return finalized_classifier

        if self._should_escalate_denial(tool_name):
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "deny",
                "Repeated denials for this tool triggered an escalation safeguard for the current session.",
                source="denial_tracking",
                metadata={"denial_count": self.consecutive_denials.get(tool_name, 0)},
            ), request)

        if not forced_prompt_reasons:
            if self.approval_policy.mode == "full":
                return self._finalize_permission_decision(tool_name, PermissionDecision(
                    "allow", "Full access selected for this conversation.", source="full_access"), request)
            for decision in (rule_decision, tool_check_decision, classifier_decision, plan_decision):
                if decision and decision.behavior == "allow":
                    return self._finalize_permission_decision(tool_name, decision, request)

        if is_read_only and not forced_prompt_reasons:
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "allow",
                "Read-only tool auto-approved.",
                source="read_only",
            ), request)

        if self.prompt_handler is None:
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "deny",
                "Permission required but no interactive approval handler is configured.",
                source="approval_missing",
            ), request)

        if forced_prompt_reasons:
            request.summary += "\nPermission policy notes:\n- " + "\n- ".join(forced_prompt_reasons)
            request.classifier_input["forced_prompt_reasons"] = forced_prompt_reasons

        await self._set_pending_request(request)
        try:
            raw_decision = self.prompt_handler(request)
            if inspect.isawaitable(raw_decision):
                raw_decision = await raw_decision
        except BaseException:
            raise
        else:
            await self._clear_pending_request()

        normalized = str(raw_decision or "").strip().lower()
        if normalized in {"allow", "y", "yes"}:
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "allow",
                "Approved for this call.",
                source="prompt",
            ), request)
        if normalized in {"always", "a"}:
            self.always_allow_tools.add(tool_name)
            await self._notify_state_change()
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "allow",
                "Approved and remembered for this session.",
                source="prompt_session_allow",
            ), request)
        if normalized in {"never", "deny_always", "block_always"}:
            self.always_deny_tools.add(tool_name)
            await self._notify_state_change()
            return self._finalize_permission_decision(tool_name, PermissionDecision(
                "deny",
                "Denied and remembered for this session.",
                source="prompt_session_deny",
            ), request)

        return self._finalize_permission_decision(tool_name, PermissionDecision(
            "deny",
            "Denied by user.",
            source="prompt",
        ), request)

    @staticmethod
    def _rule_requests(tool, request):
        # Only trusted tool classes define aliases; model arguments cannot invent them.
        yield request
        for alias, defaults in getattr(tool, "permission_aliases", {}).items():
            yield replace(request, tool_name=alias, input_payload={**defaults, **request.input_payload})

    def _inherited_restrictions(self, tool, request):
        decisions, seen, parent = [], {id(self)}, self.restriction_parent
        while parent is not None:
            if not isinstance(parent, PermissionManager) or id(parent) in seen:
                return [PermissionDecision("deny", "Invalid delegated permission ancestry.", source="delegation_policy_error")]
            seen.add(id(parent))
            mode = parent.mode_getter() if callable(parent.mode_getter) else "normal"
            inherited = replace(request, mode=mode)
            # A delegated planner may write its own plan, never other project files.
            sid = self.session_id_getter() if callable(self.session_id_getter) else ""
            plan = parent._plan_decision(inherited, session_id=sid)
            if plan and plan.behavior == "deny":
                decisions.append(plan)
            try:
                config = parent._load_config()
            except PermissionConfigError:
                return [PermissionDecision("deny", "Delegating session permission configuration is unreadable or invalid.", source="delegation_policy_error")]
            for candidate in self._rule_requests(tool, inherited):
                decision = parent._evaluate_rule_layer(candidate, config)
                if decision and decision.behavior in {"deny", "ask"}:
                    decisions.append(replace(decision, reason="Delegating session: " + decision.reason, source="delegated_" + decision.source))
                if parent._should_escalate_denial(candidate.tool_name):
                    decisions.append(PermissionDecision("deny", "Delegating session's repeated-denial safeguard applies.", source="delegated_denial_tracking"))
            parent = parent.restriction_parent
        return decisions

    def _plan_decision(self, request, *, session_id=None):
        if request.mode != "plan":
            return None
        if request.tool_name not in self.plan_mode_allowlist and not request.is_read_only:
            return PermissionDecision("deny", "Plan mode is active. Exit plan mode before making changes.", source="plan_mode")
        if request.tool_name in {"file_write_tool", "file_edit_tool"}:
            file_path = request.input_payload.get("file_path", "")
            if session_id is None:
                session_id = self.session_id_getter() if callable(self.session_id_getter) else ""
            if file_path and session_id and is_session_plan_file(os.path.abspath(file_path), session_id):
                return PermissionDecision("allow", "Plan file write auto-approved in plan mode.", source="plan_file_auto_allow")
            return PermissionDecision("deny", "Plan mode only allows editing the plan file. Exit plan mode before editing other files.", source="plan_mode_non_plan_file")
        return None

    def _is_read_only(self, tool: Any, input_payload: Dict[str, Any]) -> bool:
        inspector = getattr(tool, "is_read_only_call", None)
        if callable(inspector):
            try:
                return bool(inspector(**input_payload))
            except TypeError:
                return bool(inspector(input_payload))

        return bool(getattr(tool, "is_read_only", False))

    def _get_risk_level(self, tool: Any, input_payload: Dict[str, Any]) -> str:
        inspector = getattr(tool, "get_risk_level", None)
        if callable(inspector):
            try:
                return str(inspector(**input_payload))
            except TypeError:
                return str(inspector(input_payload))
        return str(getattr(tool, "risk_level", "high"))

    def _build_summary(self, tool: Any, input_payload: Dict[str, Any]) -> str:
        inspector = getattr(tool, "build_permission_summary", None)
        if callable(inspector):
            try:
                return str(inspector(**input_payload))
            except TypeError:
                return str(inspector(input_payload))

        preview_lines = []
        for key, value in list(input_payload.items())[:4]:
            rendered = str(value)
            if len(rendered) > 160:
                rendered = rendered[:157] + "..."
            preview_lines.append(f"{key}: {rendered}")

        description = getattr(tool, "description", "").strip()
        prefix = description if description else "Tool invocation requested."
        if not preview_lines:
            return prefix
        return prefix + "\n" + "\n".join(preview_lines)

    def _load_config(self) -> Dict[str, Any]:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError("Duplicate policy key")
                result[key] = value
            return result
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                payload = json.load(f, object_pairs_hook=unique)
            if not isinstance(payload, dict): raise ValueError("Policy must be an object")
            for key in ("alwaysDeny", "alwaysAsk", "alwaysAllow", "rules"):
                values = payload.get(key, [])
                if not isinstance(values, list): raise ValueError("Policy rules must be arrays")
                for rule in values:
                    if key != "rules" and isinstance(rule, str): continue
                    if not isinstance(rule, dict): raise ValueError("Invalid policy rule")
                    for selector in ("tools", "modes", "risk_levels"):
                        items = rule.get(selector, [])
                        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                            raise ValueError("Invalid rule selector")
                    if "read_only" in rule and not isinstance(rule["read_only"], bool): raise ValueError("Invalid read_only")
                    if key == "rules" and str(rule.get("behavior", rule.get("action", "ask"))).strip().lower() not in {
                        "allow", "approve", "accept", "ask", "prompt", "deny", "block", "reject"}:
                        raise ValueError("Invalid rule behavior")
                    matchers = rule.get("input_matches", [])
                    if not isinstance(matchers, list): raise ValueError("Invalid input matchers")
                    for matcher in matchers:
                        if not isinstance(matcher, dict): raise ValueError("Invalid matcher")
                        if "regex" in matcher:
                            if not isinstance(matcher["regex"], str): raise ValueError("Invalid regex")
                            re.compile(matcher["regex"])
            return payload
        except FileNotFoundError as error:
            if not os.path.lexists(self.config_path): return {}
            raise PermissionConfigError("Permission config is unavailable") from error
        except Exception as error:
            raise PermissionConfigError("Permission config is invalid or unreadable") from error

    def _build_classifier_input(
        self,
        *,
        tool_name: str,
        input_payload: Dict[str, Any],
        mode: str,
        risk_level: str,
        is_read_only: bool,
        summary: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "tool_name": tool_name,
            "mode": mode,
            "risk_level": risk_level,
            "is_read_only": is_read_only,
            "summary": summary,
            "input_payload": copy.deepcopy(input_payload),
            "agent_id": context.get("agent_id"),
            "agent_role": context.get("agent_role"),
            "tool_use_id": context.get("tool_use_id"),
            "turn": context.get("turn"),
        }

    def _evaluate_rule_layer(self, request: PermissionRequest, config=None) -> Optional[PermissionDecision]:
        config = self._load_config() if config is None else config
        tool_name = request.tool_name

        decisions = []
        for names, behavior in ((self.always_deny_tools, "deny"), (self.always_ask_tools, "ask"), (self.always_allow_tools, "allow")):
            if tool_name in names:
                decisions.append(PermissionDecision(behavior, "Session " + behavior + " rule matched.", source="session_rule"))
        for key, behavior in (("alwaysDeny", "deny"), ("alwaysAsk", "ask"), ("alwaysAllow", "allow")):
            decision = self._match_behavior_entries(config.get(key, []) or [], request, behavior=behavior, config_key=key)
            if decision:
                decisions.append(decision)
        aliases = {"allow": "allow", "approve": "allow", "accept": "allow", "ask": "ask", "prompt": "ask",
                   "deny": "deny", "block": "deny", "reject": "deny"}
        for rule in config.get("rules", []) or []:
            if not isinstance(rule, dict) or not self._rule_matches(rule, request):
                continue
            behavior = aliases.get(str(rule.get("behavior", rule.get("action", "ask"))).strip().lower())
            if behavior:
                decisions.append(PermissionDecision(behavior, str(rule.get("reason", "Permission rule matched.")),
                    source="config_rule", metadata={"rule": copy.deepcopy(rule)}))
        return min(decisions, key=lambda d: {"deny": 0, "ask": 1, "allow": 2}[d.behavior]) if decisions else None

    def _rule_matches(self, rule: Dict[str, Any], request: PermissionRequest) -> bool:
        tools = set(str(item) for item in (rule.get("tools") or []))
        if tools and request.tool_name not in tools:
            return False

        modes = set(str(item) for item in (rule.get("modes") or []))
        if modes and request.mode not in modes:
            return False

        risk_levels = set(str(item) for item in (rule.get("risk_levels") or []))
        if risk_levels and request.risk_level not in risk_levels:
            return False

        read_only = rule.get("read_only")
        if read_only is not None and bool(read_only) != bool(request.is_read_only):
            return False

        input_matches = rule.get("input_matches") or []
        if input_matches and not self._matches_input_filters(input_matches, request):
            return False

        return True

    def _match_behavior_entries(
        self,
        entries,
        request: PermissionRequest,
        *,
        behavior: str,
        config_key: str,
    ) -> Optional[PermissionDecision]:
        for index, entry in enumerate(entries, start=1):
            if isinstance(entry, str):
                if request.tool_name != entry:
                    continue
                return PermissionDecision(
                    behavior,
                    f"Configured {config_key} rule matched.",
                    source="config_rule",
                    metadata={
                        "config_key": config_key,
                        "entry_index": index,
                        "entry": entry,
                    },
                )
            if not isinstance(entry, dict):
                continue
            if not self._rule_matches(entry, request):
                continue
            return PermissionDecision(
                behavior,
                str(entry.get("reason", f"Configured {config_key} rule matched.")),
                source="config_rule",
                metadata={
                    "config_key": config_key,
                    "entry_index": index,
                    "entry": copy.deepcopy(entry),
                },
            )
        return None

    def _matches_input_filters(self, matchers, request: PermissionRequest) -> bool:
        for matcher in matchers:
            if not isinstance(matcher, dict):
                return False
            if not self._matches_input_filter(matcher, request):
                return False
        return True

    def _matches_input_filter(self, matcher: Dict[str, Any], request: PermissionRequest) -> bool:
        source_name = str(matcher.get("source", "input_payload") or "input_payload").strip()
        if source_name == "classifier_input":
            source_payload = request.classifier_input
        else:
            source_payload = request.input_payload

        path = matcher.get("path", matcher.get("key"))
        raw_value = self._extract_nested_value(source_payload, path)
        if raw_value is None and path not in {None, ""}:
            return False

        case_sensitive = bool(matcher.get("case_sensitive", True))
        candidate_text = self._stringify_match_value(raw_value if path not in {None, ""} else source_payload)

        glob_pattern = matcher.get("glob")
        if glob_pattern is not None:
            return self._match_glob(candidate_text, str(glob_pattern), case_sensitive=case_sensitive)

        regex_pattern = matcher.get("regex")
        if regex_pattern is not None:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                return re.search(str(regex_pattern), candidate_text, flags=flags) is not None
            except re.error:
                return False

        equals_value = matcher.get("equals", matcher.get("value"))
        if equals_value is not None:
            expected = self._stringify_match_value(equals_value)
            if case_sensitive:
                return candidate_text == expected
            return candidate_text.lower() == expected.lower()

        contains_value = matcher.get("contains")
        if contains_value is not None:
            needle = self._stringify_match_value(contains_value)
            if case_sensitive:
                return needle in candidate_text
            return needle.lower() in candidate_text.lower()

        return False

    def _extract_nested_value(self, payload: Any, path) -> Any:
        if path in {None, ""}:
            return payload
        current = payload
        for part in str(path).split("."):
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current.get(part)
                continue
            if isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
                continue
            return None
        return current

    def _stringify_match_value(self, value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    def _match_glob(self, text: str, pattern: str, *, case_sensitive: bool) -> bool:
        if case_sensitive:
            return fnmatch.fnmatchcase(text, pattern)
        return fnmatch.fnmatchcase(text.lower(), pattern.lower())

    async def _run_tool_permission_check(self, tool: Any, request: PermissionRequest) -> Optional[PermissionDecision]:
        checker = getattr(tool, "check_permissions", None)
        if not callable(checker):
            return None

        raw_decision = checker(request.classifier_input)
        if inspect.isawaitable(raw_decision):
            raw_decision = await raw_decision

        if raw_decision is None:
            return None
        if isinstance(raw_decision, PermissionDecision):
            return raw_decision
        if isinstance(raw_decision, dict):
            behavior = str(raw_decision.get("behavior", raw_decision.get("action", "ask"))).strip().lower()
            return PermissionDecision(
                behavior=behavior,
                reason=str(raw_decision.get("reason", "")),
                source=str(raw_decision.get("source", "tool_check")),
                metadata={key: value for key, value in raw_decision.items() if key not in {"behavior", "action", "reason", "source"}},
            )
        if isinstance(raw_decision, str):
            normalized = raw_decision.strip().lower()
            return PermissionDecision(normalized, "", source="tool_check")
        return None

    def _run_auto_classifier(self, request: PermissionRequest) -> Optional[PermissionDecision]:
        classifier = self.classifier_manager
        if classifier is None or not callable(getattr(classifier, "classify", None)):
            return None
        try:
            return classifier.classify(request)
        except Exception:
            return None

    def _should_escalate_denial(self, tool_name: str) -> bool:
        return int(self.consecutive_denials.get(tool_name, 0) or 0) >= self.denial_escalation_threshold

    def _finalize_permission_decision(
        self,
        tool_name: str,
        decision: PermissionDecision,
        request: PermissionRequest,
    ) -> PermissionDecision:
        behavior = str(decision.behavior or "").strip().lower()
        if behavior in {"approve", "accept"}:
            behavior = "allow"
        if behavior in {"block", "reject"}:
            behavior = "deny"
        if behavior not in {"allow", "deny", "ask"}:
            behavior = "deny"

        metadata = dict(decision.metadata or {})
        metadata["denial_count_before"] = int(self.consecutive_denials.get(tool_name, 0) or 0)
        metadata["classifier_input"] = request.classifier_input

        finalized = PermissionDecision(
            behavior=behavior,
            reason=decision.reason,
            source=decision.source,
            metadata=metadata,
        )

        if behavior == "allow":
            self.consecutive_denials[tool_name] = 0
            return finalized

        if behavior == "deny" and decision.source not in {"config_error", "delegation_policy_error"}:
            self._record_denial(tool_name, finalized)
            finalized.metadata["denial_count_after"] = int(self.consecutive_denials.get(tool_name, 0) or 0)
            return finalized

        return finalized

    def _record_denial(self, tool_name: str, decision: PermissionDecision):
        count = int(self.consecutive_denials.get(tool_name, 0) or 0) + 1
        self.consecutive_denials[tool_name] = count
        self.denial_history.insert(0, {
            "tool_name": tool_name,
            "reason": decision.reason,
            "source": decision.source,
            "count": count,
        })
        self.denial_history = self.denial_history[:20]

    async def _set_pending_request(self, request: PermissionRequest):
        self.pending_request = {
            "tool_name": request.tool_name,
            "summary": request.summary,
            "risk_level": request.risk_level,
            "input_payload": copy.deepcopy(request.input_payload),
            "mode": request.mode,
            "is_read_only": request.is_read_only,
            "tool_description": request.tool_description,
            "classifier_input": copy.deepcopy(request.classifier_input),
            "denial_count": request.denial_count,
        }
        await self._notify_state_change()

    async def _clear_pending_request(self):
        if self.pending_request is None:
            return
        self.pending_request = None
        await self._notify_state_change()

    async def _notify_state_change(self):
        if not callable(self.state_change_callback):
            return
        result = self.state_change_callback()
        if inspect.isawaitable(result):
            await result
