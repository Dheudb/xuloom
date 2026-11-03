import asyncio
import copy
import hashlib
import json
import time
import uuid
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.agents.registry import AgentRegistry
from xuloom.agents.tasks import SubagentTasks
from xuloom.agents.lifecycle import AgentLifecycle, concurrency_limit
from xuloom.agents.role_runtime import ALL_AGENT_DISALLOWED_TOOLS, configure_role, filter_tools
from xuloom.skills.registry import SkillRegistry
from xuloom.agents.pipeline import PipelineItem, PipelineStage

MAX_SUBAGENT_DEPTH = 2


class AgentToolInput(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["spawn", "capacity", "list", "result", "wait", "send", "followup", "resume_queue", "cancel", "close", "pipeline", "tree"] = "spawn"
    agent_ids: Optional[List[str]] = Field(None, description="Worker IDs to join; terminal failures count as finished, not successful")
    wait_for: Optional[Literal["any", "all"]] = Field(None, description="Explicit join condition; does not cancel remaining workers. Legacy default: JSON any, text all")
    run_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,128}$", description="Stable pipeline ID; repeat to resume verified steps without replaying interrupted operations")
    items: Optional[List[PipelineItem]] = None
    stages: Optional[List[PipelineStage]] = None
    timeout_ms: int = Field(30000, ge=0, le=60000, description="Bounded wait timeout")
    response_format: str = Field("text", pattern="^(text|json)$", description="Use json for structured spawn receipts and wait results")
    request_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,128}$", description="Stable caller-supplied spawn identity. Repeating the same request returns its existing workers; changed arguments are rejected")
    reasoning_effort: Optional[str] = Field(None, pattern="^(none|minimal|low|medium|high|xhigh|max)$")
    agent_id: Optional[str] = Field(None, description="Background subagent ID for result or cancel")
    description: str = Field("", description="A short description of the task")
    prompt: str = Field("", description="The task for the agent to perform")
    count: int = Field(1, ge=1, le=10, strict=True, description="Number of independent agents given this same prompt in parallel. For ten greeting/comparison agents, use count=10 in ONE call.")
    subagent_type: Optional[str] = Field(
        None,
        description=(
            "Optional registered role from the supplied enum. Omit for general-purpose behavior."
        ),
    )
    model: Optional[str] = Field(
        None,
        description="Optional model override for this agent. If omitted, inherits from the parent.",
    )
    run_in_background: bool = Field(
        True,
        description="Defaults to true: launch without blocking the parent or peer workers; collect results before final synthesis. Explicit false waits inside this call.",
    )
    inherit_context: bool = Field(False, description="Include the parent conversation as reference only. Defaults to an independent task context.")


class SpawnAgentInput(BaseModel):
    model_config = {"extra": "forbid"}
    prompt: str = Field(..., min_length=1, description="Self-contained assignment, constraints and desired output")
    description: str = Field("", description="Short task label")
    subagent_type: Optional[str] = Field(None, description="Optional registered role; defaults to general-purpose")
    inherit_context: bool = Field(False, description="Include parent history as reference; otherwise provide needed context in prompt")
    count: int = Field(1, ge=1, le=10, strict=True, description="Use more than one only for explicitly requested repeated independent trials of this same assignment")

class AgentTool(AgentLifecycle, BaseAgenticTool):
    name = "agent_tool"
    description = "Delegate a bounded independent assignment to a worker; launch, message, wait for, or continue agents using the selected collaboration policy."
    input_schema = AgentToolInput
    risk_level = "medium"

    def _roles(self):
        registry = self.context.get("agent_registry") or AgentRegistry(self.context.get("session_cwd"))
        return registry.definitions()

    def get_tool_definition(self):
        definition = super().get_tool_definition()
        field = definition["input_schema"]["properties"]["subagent_type"]
        for option in field.get("anyOf", [field]):
            if option.get("type") == "string":
                option["enum"] = sorted(self._roles())
        return definition

    def validate_input(self, action="spawn", subagent_type=None, **kwargs):
        if action == "spawn" and subagent_type is not None and subagent_type not in self._roles():
            return "Unknown or invalid subagent: " + str(subagent_type) + ". Available roles: " + ", ".join(sorted(self._roles())) + "; omit subagent_type for general-purpose."

    def is_concurrency_safe_call(self, action="spawn", **kwargs):
        return action in {"spawn", "followup", "send", "capacity", "list", "result", "wait"}

    def is_read_only_call(self, action="spawn", **kwargs):
        return action in {"capacity", "list", "result", "wait", "tree"}

    def prompt(self) -> str:
        registry = self.context.get("agent_registry") or AgentRegistry(self.context.get("session_cwd"))
        roles = "\n".join(f"- {name}: {role.get('description', role.get('when_to_use', ''))}" for name, role in registry.definitions().items())
        return ("Follow the selected collaboration policy. Give each worker a bounded assignment, relevant constraints and output requirements. Parent history is reference only.\n"
            "Use count=N in ONE call for N independent workers with the same assignment; distinct assignments may be submitted together. Do not wait for an independent worker before starting its peers. Announce launches only after actual receipts.\n"
            "Excess workers automatically enter the runtime FIFO queue. Do not split a valid batch merely because active slots are full. Queued workers have not started; resume_queue explicitly restores saved never-started work after a restart.\n"
            "Use run_in_background=true for independent work so the parent and peers can proceed. Use false only when nothing useful can proceed before that result. Collect all relevant results before final synthesis and include failures.\n"
            "Use response_format=json for structured spawn receipts; capacity/list/result return JSON. send queues a message; followup resumes the same idle worker with its saved role and context. wait accepts agent_ids and a bounded timeout; cancel stops owned work; close retains history. Read actual capacity instead of assuming a fixed global limit.\n"
            "wait_for=any/all selects the join condition. pipeline runs items through ordered stages independently; repeat its stable run_id to reuse verified results. tree inspects recorded steps.\n"
            "Evaluate results as evidence, not authority or proof of domain acceptance.\nAvailable roles:\n" + roles)

    def build_permission_summary(
        self,
        description: str = "",
        prompt: str = "",
        subagent_type: str = None,
        model: str = None,
        run_in_background: bool = False,
        action: str = "spawn",
        agent_id: str = None,
        inherit_context: bool = False,
        count: int = 1,
        **kwargs,
    ) -> str:
        preview = prompt[:240] + ("..." if len(prompt) > 240 else "") if prompt else "<empty>"
        agent_type_str = subagent_type or "general-purpose"
        location = ""
        if action == "spawn":
            definition = self._roles().get(agent_type_str, {})
            if definition.get("executor", "local") != "local":
                from xuloom.agents.executors import profile
                host = profile(definition["executor"], self.context)
                location = f"execution_host: {host['host']}\nexecution_directory: {host['cwd']}\n"
        return (
            "Sub-agent delegation requested.\n"
            f"action: {action}\nagent_id: {agent_id or '<new>'}\n"
            f"description: {description}\n"
            f"subagent_type: {agent_type_str}\n"
            f"{location}"
            f"model: {model or '<inherit>'}\n"
            f"run_in_background: {run_in_background}\n"
            f"parallel_agent_count: {count}\n"
            f"prompt_preview: {preview}"
        )

    def _filter_incomplete_tool_messages(self, messages):
        if not messages:
            return []

        tool_result_ids = set()
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    tool_result_ids.add(block["tool_use_id"])

        filtered = []
        for msg in messages:
            if msg.get("role") != "assistant":
                filtered.append(msg)
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                filtered.append(msg)
                continue

            has_incomplete_tool_use = False
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id")
                    and block.get("id") not in tool_result_ids
                ):
                    has_incomplete_tool_use = True
                    break

            if not has_incomplete_tool_use:
                filtered.append(msg)

        return filtered
    
    def _filter_tools(
        self,
        parent_tools,
        allowed_tools: List[str],
        disallowed_tools: set = None,
    ):
        return filter_tools(parent_tools, allowed_tools, disallowed_tools)

    def _share_runtime_managers(self, sub_engine):
        shared_manager_fields = [
            "session_manager",
            "lsp_manager",
            "sandbox_manager",
            "browser_manager",
            "shell_task_manager",
            "incremental_write_queue",
            "memory_file_manager",
            "artifact_tracker",
            "content_replacement_manager",
            "command_sessions",
        ]

        for field in shared_manager_fields:
            manager = self.context.get(field)
            if manager is None:
                continue
            setattr(sub_engine, field, manager)
            sub_engine.tool_context[field] = manager

    def _configure_subagent_permissions(self, sub_engine):
        parent_permission_manager = self.context.get("permission_manager")
        if parent_permission_manager is None:
            return

        # Child agents keep the same approval UX, but start with a clean
        # permission memory so parent session-wide allow rules do not leak.
        sub_engine.permission_manager.always_allow_tools = set()
        sub_engine.permission_manager.restriction_parent = parent_permission_manager
        sub_engine.permission_manager.approval_policy = parent_permission_manager.approval_policy
        sub_engine.tool_context["permission_manager"] = sub_engine.permission_manager
        sub_engine.tool_context["permission_handler"] = self.context.get("permission_handler")

    async def _write_subagent_registry_record(self, record: dict):
        registry_writer = self.context.get("register_subagent_record")
        if not callable(registry_writer):
            return

        lock = self.context.get("subagent_state_lock")
        if lock is None:
            if registry_writer(record) is False:
                raise OSError("Could not persist subagent registry")
            return

        async with lock:
            if registry_writer(record) is False:
                raise OSError("Could not persist subagent registry")

    async def _run_single_subagent(
        self,
        task: str,
        allowed_tools: List[str],
        inherit_context: bool,
        parent_depth: int,
        parent_agent_id: str,
        agent_role: str = "subagent",
        builtin_agent_def: dict = None,
        model: str = None,
        child_agent_id: str = None,
        background: bool = False,
        description: str = "",
        resume_state: dict = None,
        connection_binding: dict = None,
        owner_turn_id: str = None,
    ) -> dict:
        from xuloom.core.engine import QueryEngine

        effective_role = agent_role
        if builtin_agent_def:
            effective_role = builtin_agent_def.get("agent_role", agent_role)

        child_agent_id = child_agent_id or f"{effective_role}-{uuid.uuid4()}"
        child_model = model or (builtin_agent_def or {}).get("model") or self.context.get("primary_model")
        options = self.context["model_client_options"]() if callable(self.context.get("model_client_options")) else {}
        provider, base = self.context.get("model_provider"), self.context.get("api_base_url")
        if connection_binding is not None:
            from xuloom.agents.connections import resolve_connection
            binding, options = resolve_connection(connection_binding["connection_id"], connection_binding["model"], frozen=connection_binding)
            child_model, provider, base = binding["model"], binding["provider"], binding["api_base_url"]
        same_model = connection_binding is None and child_model == self.context.get("primary_model")
        input_contract = self.context.get("model_input_contract")
        inherited_contract = {"inputs": input_contract(child_model), "reasoning": copy.deepcopy(self.context.get("reasoning_profile"))} if same_model and callable(input_contract) else None
        if inherited_contract is not None and callable(self.context.get("context_profile")):
            inherited_contract["context"] = copy.deepcopy(self.context["context_profile"]())
        if inherited_contract is not None and callable(self.context.get('native_search_options')):
            inherited_contract['search'] = copy.deepcopy(self.context['native_search_options']()['profile'])
        sub_engine = QueryEngine(
            model=child_model,
            fallback_model=child_model if connection_binding is not None else self.context.get("fallback_model"),
            permission_handler=self.context.get("permission_handler"),
            agent_id=child_agent_id,
            parent_agent_id=parent_agent_id,
            agent_depth=parent_depth + 1,
            agent_role=effective_role,
            model_provider=provider,
            api_base_url=base,
            local_tokenizer_path=None if connection_binding is not None else self.context.get("local_tokenizer_path"),
            cwd=self.context.get("session_cwd"),
            thinking_config=self.context.get("thinking_config") if same_model else None,
            mode_thinking_overrides=self.context.get("mode_thinking_overrides") if same_model else None,
            reasoning_effort=(builtin_agent_def or {}).get("reasoning_effort") or (self.context.get("reasoning_effort") if same_model else None),
            collaboration_mode="solo",
            execution_mode=getattr(self.context.get("execution_sandbox"), "mode", "host"),
            model_contract=connection_binding["contract"] if connection_binding is not None else inherited_contract,
            **options,
        )
        if same_model and self.context.get("reasoning_profile") is not None:
            from xuloom.core.reasoning import request_options
            sub_engine.reasoning_profile = copy.deepcopy(self.context["reasoning_profile"])
            request_options(sub_engine.reasoning_profile, sub_engine.model_provider, effort_override=sub_engine.reasoning_effort)
            sub_engine._sync_model_runtime_context()
        sub_engine._thinking_config_explicit = same_model and self.context.get("thinking_config_explicit", False)
        sub_engine.tool_context["thinking_config_explicit"] = sub_engine._thinking_config_explicit
        definition = copy.deepcopy(builtin_agent_def or {})
        from xuloom.context.tool_media import artifact_owner
        sub_engine.tool_context["delegation"] = {"owner_session_id": self.context.get("session_id"), "owner_turn_id": owner_turn_id, "role_definition": definition,
            "connection_binding": copy.deepcopy(connection_binding), "root_session_id": artifact_owner(self.context)}
        sub_engine.tool_context["delegated_mailbox"] = lambda: self._mailbox(child_agent_id)
        sub_engine.tool_context["ack_delegated_mailbox"] = lambda ids: self._ack_mailbox(child_agent_id, ids)
        self._configure_subagent_permissions(sub_engine)
        if self.context.get("plan_manager"):
            sub_engine.plan_manager.set_mode(self.context["plan_manager"].get_mode())

        self._share_runtime_managers(sub_engine)
        if resume_state:
            from xuloom.features.session_summary import merge_references
            merge_references(sub_engine.artifact_tracker.references,
                resume_state.get("metadata", {}).get("artifacts", {}).get("references", {}), agent_id=child_agent_id)
        if self.context.get("goal_budget"):
            sub_engine.tool_context["goal_budget"] = self.context["goal_budget"]
        sub_engine.tool_context["skill_registry"] = self.context.get("skill_registry") or SkillRegistry(self.context.get("session_cwd"))
        sub_engine.tool_context["ask_user_callback"] = self.context.get("ask_user_callback")
        if self.context.get("browser_manager"):
            sub_engine.browser_manager.capture_callback = self.context["browser_manager"].capture_callback
        sub_engine.tool_context["capability_event"] = self.context.get("capability_event")
        sub_engine.tool_context["plan_approval_callback"] = self.context.get("plan_approval_callback")
        team = self.context.get("team_manager")
        if team and team.is_active:
            team.register_member(child_agent_id, child_agent_id, effective_role, sub_engine.session_cwd, sub_engine.primary_model)
            sub_engine.team_manager = team
            sub_engine.tool_context["team_manager"] = team
            sub_engine.tool_context["team_member_name"] = child_agent_id

        latent = self.context.get("latent_tools_registry", {})
        team_tool = None
        if team and team.is_active:
            team_tool = self.context.get("engine_available_tools", {}).get("send_message_tool") or latent.get("send_message_tool")
        try:
            configure_role(sub_engine, definition, allowed_tools=allowed_tools,
                parent_tools=self.context.get("engine_available_tools"), latent_tools=latent,
                skills=sub_engine.tool_context["skill_registry"], team_tool=team_tool,
                context_messages=self._filter_incomplete_tool_messages(self.context.get("messages", [])) if inherit_context else None,
                frozen_prompt=(resume_state or {}).get("metadata", {}).get("delegation", {}).get("system_prompt") or None)
        except BaseException:
            await sub_engine.aclose()
            raise
        if resume_state:
            sub_engine.messages = copy.deepcopy(resume_state["messages"])
            metadata = resume_state.get("metadata", {})
            from xuloom.agents.external.runtime import restore as restore_external
            try:
                restore_external(sub_engine, metadata)
            except BaseException:
                await sub_engine.aclose()
                raise
            sub_engine.permission_manager.load_state({**metadata.get("permissions", {}), "always_allow_tools": [], "pending_request": None})
            from xuloom.core.tool_execution import restore_tool_activity, restore_tool_catalog
            restore_tool_activity(sub_engine, metadata)
            restore_tool_catalog(sub_engine, metadata)
            sub_engine.tool_context["file_changes"] = metadata.get("file_changes", [])
            sub_engine.turn_traces = metadata.get("turn_traces", [])
            sub_engine.todo_manager.load(metadata.get("todos", []))
            frozen_prompt = metadata.get("delegation", {}).get("system_prompt")
            if frozen_prompt:
                sub_engine.tool_context["agent_system_prompt"] = frozen_prompt
            thinking = metadata.get("thinking", {})
            if thinking.get("default"):
                sub_engine.set_thinking_config(thinking["default"])
            if thinking.get("mode_overrides"):
                sub_engine.mode_thinking_overrides = copy.deepcopy(thinking["mode_overrides"])
            sub_engine._update_compact_boundary()
            sub_engine._sync_message_context()
        sub_engine.tool_context["delegation"]["system_prompt"] = sub_engine.tool_context["agent_system_prompt"]
        user_input_text = task
        completed = False
        from xuloom.agents.external.runtime import recovery_status
        registry_record = {"agent_id": child_agent_id}
        sub_engine.tool_context["checkpoint_model_budget"] = lambda: self._write_subagent_registry_record(registry_record)

        try:
            session_manager = self.context.get("session_manager")
            transcript_path = (
                session_manager.get_subagent_transcript_path(child_agent_id)
                if session_manager else None
            )
            registry_record = {
                "agent_id": child_agent_id,
                "parent_agent_id": parent_agent_id,
                "agent_depth": parent_depth + 1,
                "agent_role": effective_role,
                "agent_type": builtin_agent_def.get("agent_type") if builtin_agent_def else None,
                "status": "running",
                "phase": "running",
                "task": task[:240] + ("..." if len(task) > 240 else ""),
                "description": description,
                "transcript_path": transcript_path,
                "model": sub_engine.primary_model,
                "background": background,
            }
            await self._write_subagent_registry_record(registry_record)
            if session_manager:
                saved = session_manager.save_subagent_transcript(child_agent_id, {
                    "agent_id": child_agent_id,
                    "parent_agent_id": parent_agent_id,
                    "agent_depth": parent_depth + 1,
                    "agent_role": effective_role,
                    "task": task,
                    "allowed_tools": allowed_tools,
                    "inherit_context": inherit_context,
                    "status": "running",
                    "messages": sub_engine.messages,
                    "metadata": sub_engine._build_session_metadata(),
                })
                if not saved:
                    raise OSError("Could not persist initial subagent transcript; model was not started")

            res = await sub_engine.run(
                user_input=user_input_text,
                sys_print_callback=lambda x: None
            )
            from xuloom.core.run_result import RunResult
            if not isinstance(res, RunResult):
                raise TypeError("Engine returned no structured execution receipt")
            receipt = res.to_dict()
            if res.ok and not res.text.strip():
                receipt.update(status="crashed", ok=False, reason="empty_result")
            if session_manager:
                saved = session_manager.save_subagent_transcript(child_agent_id, {
                    "agent_id": child_agent_id, "parent_agent_id": parent_agent_id,
                    "agent_depth": parent_depth + 1, "agent_role": effective_role,
                    "session_id": self.context.get("session_id"),
                    "task": task, "allowed_tools": allowed_tools, "inherit_context": inherit_context,
                    "status": receipt["status"], "transcript_path": transcript_path,
                    "messages": sub_engine.messages, "metadata": sub_engine._build_session_metadata(),
                    "result": res.text, "receipt": receipt,
                })
                if not saved:
                    receipt.update(status="crashed", ok=False, persisted=False, reason="persistence_error")
            payload = {"task": task, "agent_id": child_agent_id, "agent_role": effective_role,
                       "status": receipt["status"], "phase": receipt["status"], "result": res.text, "receipt": receipt,
                       "external_recovery": recovery_status(sub_engine.tool_context.get("external_agent")) if not receipt["ok"] else None}
            await self._write_subagent_registry_record({**registry_record, **payload, "finished_at": int(time.time() * 1000)})
            completed = receipt["ok"]
            return payload
        except asyncio.CancelledError:
            await self._write_subagent_registry_record({"agent_id": child_agent_id, "status": "cancelled", "finished_at": int(time.time() * 1000),
                "external_recovery": recovery_status(sub_engine.tool_context.get("external_agent"))})
            raise
        except Exception as e:
            receipt = {"status": "crashed", "ok": False, "persisted": False, "reason": "persistence_error" if isinstance(e, OSError) else "execution_error"}
            session_manager = self.context.get("session_manager")
            if session_manager:
                session_manager.save_subagent_transcript(child_agent_id, {
                    "agent_id": child_agent_id,
                    "parent_agent_id": parent_agent_id,
                    "agent_depth": parent_depth + 1,
                    "agent_role": effective_role,
                    "task": task,
                    "allowed_tools": allowed_tools,
                    "inherit_context": inherit_context,
                    "status": "crashed",
                    "error": str(e),
                    "messages": sub_engine.messages,
                    "metadata": sub_engine._build_session_metadata(),
                })
            await self._write_subagent_registry_record({
                **registry_record,
                "status": "crashed",
                "receipt": receipt,
                "result": str(e),
                "external_recovery": recovery_status(sub_engine.tool_context.get("external_agent")),
                "finished_at": int(time.time() * 1000),
            })
            return {
                "task": task,
                "agent_id": child_agent_id,
                "agent_role": effective_role,
                "status": "crashed",
                "receipt": receipt,
                "result": f"Exception Fault: Sub-Agent terminal crash: {e}",
            }
        finally:
            if team and team.is_active:
                team.deactivate_member(child_agent_id)
            if not completed and sub_engine.tool_context.get("command_sessions"):
                await sub_engine.tool_context["command_sessions"].close(owner=child_agent_id)
            if not completed:
                await sub_engine.shell_task_manager.close(owner=child_agent_id)
            await sub_engine.client.close()
            if getattr(sub_engine.compactor, "client", None):
                await sub_engine.compactor.client.close()

    def _format_single_report(self, payload: dict) -> str:
        uncertainty = "Remote execution or resource cleanup still requires confirmation.\n" if payload.get("outcome_unknown") else ""
        return (
            "=======[ SUB-AGENT COMPLETION REPORT ]=======\n"
            f"Agent ID: {payload.get('agent_id')}\n"
            f"Role: {payload.get('agent_role', 'subagent')}\n"
            f"Task Handled: {payload.get('task')}\n"
            f"Status: {payload.get('status')}\n\n"
            f"{uncertainty}"
            f"Outcome / Findings:\n{payload.get('result')}\n"
            "==============================================="
        )

    def _format_batch_report(self, payloads: List[dict], parallel: bool) -> str:
        completed = sum(1 for item in payloads if item.get("status") == "completed")
        crashed = sum(1 for item in payloads if item.get("status") == "crashed")
        header = (
            "=======[ SUB-AGENT BATCH REPORT ]=======\n"
            f"Execution mode: {'parallel' if parallel else 'serial'}\n"
            f"Task count: {len(payloads)}\n"
            f"Completed: {completed}\n"
            f"Crashed: {crashed}\n"
            "========================================\n"
        )
        body = "\n\n".join(self._format_single_report(item) for item in payloads)
        return header + "\n" + body

    async def execute(
        self,
        description: str = "",
        prompt: str = "",
        subagent_type: str = None,
        model: str = None,
        run_in_background: bool = False,
        action: str = "spawn",
        agent_id: str = None,
        inherit_context: bool = False,
        count: int = 1,
        agent_ids: List[str] = None,
        timeout_ms: int = 30000,
        response_format: str = "text",
        reasoning_effort: str = None,
        request_id: str = None,
        wait_for: str = None,
        run_id: str = None,
        items: List[dict] = None,
        stages: List[dict] = None,
    ) -> str:
        limit = concurrency_limit(self.context)
        tasks = self.context.setdefault("subagent_tasks", SubagentTasks(limit))
        if not any(not task.done() for task in tasks.tasks.values()):
            tasks.limit = limit
        if not 0 <= timeout_ms <= 60000:
            raise ValueError("timeout_ms must be between 0 and 60000")
        if action == "tree":
            from xuloom.agents.pipeline import tree
            return json.dumps(tree(self.context, run_id), ensure_ascii=False)
        if action == "pipeline":
            from xuloom.agents.pipeline import execute
            return json.dumps(await execute(self, run_id, items, stages), ensure_ascii=False)
        if action == "wait":
            joined = await self._manage_worker(action, agent_id, prompt, timeout_ms, agent_ids,
                run_in_background, wait_for=wait_for or ("all" if response_format == "text" else "any"))
            if response_format == "json" or wait_for is not None or agent_ids:
                return joined
            outcome = json.loads(joined)
            if not outcome["condition_met"]:
                return joined
            return self._format_batch_report(outcome["agents"], parallel=True)
        if action != "spawn":
            return await self._manage_worker(action, agent_id, prompt, timeout_ms, agent_ids, run_in_background)
        if action == "list":
            return str(self.context.get("subagent_registry", []))
        if action == "result":
            saved = next((record for record in self.context.get("subagent_registry", []) if record.get("agent_id") == agent_id), None)
            return str(tasks.results.get(agent_id) or saved or {"agent_id": agent_id, "status": "running" if agent_id in tasks.tasks else "unknown"})
        if action == "cancel":
            return str(await tasks.cancel(agent_id))
        if action != "spawn":
            raise ValueError("Unknown subagent action")
        if type(count) is not int or not 1 <= count <= 10:
            raise ValueError("Subagent count must be an integer from 1 to 10")
        if not prompt:
            return "Sub-agent spawn denied: no prompt was provided."

        fingerprint = hashlib.sha256(json.dumps([prompt, description, subagent_type or "general-purpose", model,
            run_in_background, inherit_context, count, reasoning_effort], ensure_ascii=False).encode()).hexdigest()
        if request_id:
            existing = [row for row in self.context.get("subagent_registry", []) if row.get("request_id") == request_id]
            if existing:
                if len(existing) != count or any(row.get("request_digest") != fingerprint for row in existing):
                    raise ValueError("Spawn request already exists with different arguments or incomplete registration; inspect its actual workers before continuing")
                rows = [row for row in self._inventory() if row["agent_id"] in {saved["agent_id"] for saved in existing}]
                return json.dumps({"agents": rows, "reused": True, "request_id": request_id}, ensure_ascii=False)

        parent_depth = int(self.context.get("agent_depth", 0) or 0)
        if parent_depth >= MAX_SUBAGENT_DEPTH:
            return (
                f"Sub-agent spawn denied: maximum depth {MAX_SUBAGENT_DEPTH} reached. "
                "Complete the remaining work in the current agent."
            )

        registry = self.context.get("agent_registry") or AgentRegistry(self.context.get("session_cwd"))
        builtin_def = registry.resolve(subagent_type or "general-purpose")
        builtin_def = copy.deepcopy(builtin_def)
        builtin_def["disallowed_tools"] = list(builtin_def.get("disallowed_tools", []))
        from xuloom.agents.executors import executor, profile as execution_profile
        location = executor(builtin_def.get("executor"))
        remote = location != "local"
        if remote:
            execution_profile(location, self.context)
            builtin_def["model"] = model or builtin_def.get("model")
        elif builtin_def.get("backend", "native") != "native":
            from xuloom.agents.external.program import resolve
            builtin_def["_external_binding"] = resolve(builtin_def.get("backend_program"), backend=builtin_def["backend"], workspace=self.context.get("session_cwd"))
        binding = None
        if not remote and builtin_def.get("connection"):
            from xuloom.agents.connections import resolve_connection
            binding, _ = resolve_connection(builtin_def["connection"], model or builtin_def.get("model"))
        if not remote and binding is None and (model or builtin_def.get("model") or self.context.get("primary_model")) == self.context.get("primary_model"):
            builtin_def["reasoning_effort"] = builtin_def.get("reasoning_effort") or self.context.get("reasoning_effort")
        if reasoning_effort:
            from xuloom.core.reasoning import EFFORTS
            if reasoning_effort not in EFFORTS:
                raise ValueError("Unsupported reasoning_effort")
            builtin_def["reasoning_effort"] = reasoning_effort
        from xuloom.core.reasoning import configured_profile, request_options
        target_model = builtin_def.get("model") if remote else binding["model"] if binding else model or builtin_def.get("model") or self.context.get("primary_model")
        target_profile = None if remote else binding["contract"]["reasoning"] if binding else (self.context.get("reasoning_profile") if target_model == self.context.get("primary_model")
            else configured_profile(self.context.get("model_provider"), target_model, self.context.get("api_base_url")))
        if target_profile is not None:
            request_options(target_profile, binding["provider"] if binding else self.context.get("model_provider"), effort_override=builtin_def.get("reasoning_effort"))
        elif builtin_def.get("reasoning_effort") and binding and binding["provider"] != "openai":
            raise ValueError("Anthropic effort requires an explicit model reasoning profile")
        parent_agent_id = self.context.get("agent_id")

        allowed = builtin_def.get("allowed_tools")
        if not remote and allowed and "*" not in allowed:
            available = {**self.context.get("engine_available_tools", {}), **self.context.get("latent_tools_registry", {})}
            unknown = set(allowed) - set(available)
            if unknown:
                raise ValueError("Subagent requires unavailable tools: " + ", ".join(sorted(unknown)))
        # Check the complete batch before registering or scheduling any workers.
        # launch_batch checks again without yielding, so concurrent calls cannot
        # partially launch an over-capacity batch.
        from xuloom.agents.collaboration import require_delegation
        require_delegation(self.context)
        tasks.check_capacity(count)
        from xuloom.agents.collaboration import admit
        admit(self.context, count)
        from xuloom.features.session_summary import human_turn
        owner_turn_id = (self.context.get("delegation") or {}).get("owner_turn_id") or human_turn(self.context.get("messages", []))
        records = [{
            "agent_id": "subagent-" + str(uuid.uuid4()),
            "parent_agent_id": parent_agent_id,
            "owner_turn_id": owner_turn_id,
            "agent_depth": parent_depth + 1,
            "agent_role": builtin_def.get("agent_role", "subagent"),
            "agent_type": builtin_def.get("agent_type", subagent_type or "general-purpose"),
            "status": "queued", "phase": "queued",
            "task": prompt[:240] + ("..." if len(prompt) > 240 else ""),
            "description": f"{description or '子代理'} {index + 1}/{count}" if count > 1 else description,
            "model": target_model,
            "executor": location,
            "backend": builtin_def.get("backend", "native"),
            "connection_binding": copy.deepcopy(binding),
            "background": run_in_background,
            "role_definition": copy.deepcopy(builtin_def),
            "turn": 1,
            "pending_messages": [],
            "request_id": request_id, "request_digest": fingerprint if request_id else None,
            "queued_at": int(time.time() * 1000), "started_at": None,
            "admission_run_id": self.context["collaboration_run"]["id"],
            "dispatch": {"task": prompt, "allowed_tools": allowed, "inherit_context": inherit_context,
                "parent_depth": parent_depth, "parent_agent_id": parent_agent_id, "builtin_agent_def": copy.deepcopy(builtin_def),
                "model": target_model, "background": run_in_background, "connection_binding": copy.deepcopy(binding)},
            "provider": None if remote else binding["provider"] if binding else self.context.get("model_provider"),
            "api_base_url": None if remote else binding["api_base_url"] if binding else self.context.get("api_base_url"),
        } for index in range(count)]
        if remote:
            from xuloom.agents.remote_worker import prepare
            reference = self._filter_incomplete_tool_messages(self.context.get("messages", [])) if inherit_context else None
            for record in records:
                record["dispatch"]["remote"] = prepare(self.context, builtin_def, record, reference)
        # Register every card before any child can make a model request.
        try:
            for record in records:
                await self._write_subagent_registry_record(record)
            coroutines = {record["agent_id"]: self._run_registered_subagent(
                record, task=prompt,
                allowed_tools=allowed,
                inherit_context=inherit_context,
                parent_depth=parent_depth,
                parent_agent_id=parent_agent_id,
                builtin_agent_def=builtin_def,
                model=model,
                connection_binding=copy.deepcopy(binding),
                child_agent_id=record["agent_id"],
                background=run_in_background,
                description=record["description"],
            ) for record in records}
            tasks.launch_batch(coroutines, on_cancelled_before_start=self.context.get("register_subagent_record"))
            if run_in_background:
                if response_format == "json":
                    identities = {row["agent_id"] for row in records}
                    return json.dumps({"agents": [row for row in self._inventory() if row["agent_id"] in identities]}, ensure_ascii=False)
                if count == 1:
                    return "Background subagent accepted (automatically queued when slots are full). agent_id=" + records[0]["agent_id"]
                return "Background subagents accepted; runtime automatically queues excess workers. count={}\n{}".format(count, "\n".join(record["agent_id"] for record in records))
            payloads = await asyncio.gather(*(tasks.tasks[row["agent_id"]] for row in records))
            tasks.delivered.update(row["agent_id"] for row in records)
            if response_format == "json":
                return json.dumps({"agents": payloads}, ensure_ascii=False)
            return self._format_single_report(payloads[0]) if count == 1 else self._format_batch_report(payloads, parallel=True)
        except BaseException as exc:
            terminal_ids = {item["agent_id"] for item in self.context.get("subagent_registry", [])
                            if item.get("status") not in {"running", "queued"}}
            for record in records:
                if record["agent_id"] in terminal_ids:
                    continue
                await self._write_subagent_registry_record({"agent_id": record["agent_id"],
                    "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "crashed",
                    "finished_at": int(time.time() * 1000), "result": str(exc)})
            raise

    async def _run_registered_subagent(self, record, **kwargs):
        # Initialization can fail before the engine's run try/finally starts.
        # Every allocated worker must still reach a terminal status.
        try:
            await self._write_subagent_registry_record({"agent_id": record["agent_id"], "status": "running",
                "phase": "starting", "started_at": int(time.time() * 1000), "finished_at": None})
            from xuloom.agents.collaboration import DELEGATION_BUDGET
            token = DELEGATION_BUDGET.set(self.context.get("collaboration_run"))
            try:
                if record.get("executor", "local") != "local":
                    from xuloom.agents.remote_worker import RemoteWorker
                    controls = self.context["subagent_tasks"].backends
                    handle = controls.setdefault(record["agent_id"], RemoteWorker(self, record["agent_id"]))
                    payload = await handle.run()
                else:
                    payload = await self._run_single_subagent(**kwargs, owner_turn_id=record.get("owner_turn_id"))
            finally:
                DELEGATION_BUDGET.reset(token)
        except asyncio.CancelledError:
            if self._worker(record["agent_id"]).get("status") != "cancelled":
                await self._write_subagent_registry_record({"agent_id": record["agent_id"],
                    "status": "cancelled", "finished_at": int(time.time() * 1000)})
            raise
        except Exception as exc:
            payload = {"agent_id": record["agent_id"], "agent_role": record["agent_role"],
                "task": kwargs["task"], "status": "crashed", "result": str(exc)}
            await self._write_subagent_registry_record({**payload, "finished_at": int(time.time() * 1000)})
        return payload


class SpawnAgentTool(AgentTool):
    """Small model-facing launch surface over the existing worker runtime."""
    name = "spawn_agent"
    description = "Start a subagent on a bounded independent task in the background, then continue your own work. Returns actual worker IDs."
    input_schema = SpawnAgentInput
    permission_aliases = {"agent_tool": {"action": "spawn", "run_in_background": True}}

    def prompt(self):
        return "Launch independent branches early under the selected collaboration policy. Use agent_tool for wait, results, messaging and cancellation; await relevant results before final synthesis."

    def build_permission_summary(self, **kwargs):
        return super().build_permission_summary(run_in_background=True, **kwargs)

    async def execute(self, prompt, description="", subagent_type=None, inherit_context=False, count=1):
        return await super().execute(action="spawn", prompt=prompt, description=description,
            subagent_type=subagent_type, inherit_context=inherit_context, count=count,
            run_in_background=True, response_format="json")
