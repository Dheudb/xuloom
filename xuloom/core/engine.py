import os
import asyncio
import copy
import inspect
import json
import logging
import re
import sys
import time
from pathlib import Path
from xuloom.branding import configuration_file
from types import SimpleNamespace
from typing import Optional

_logger = logging.getLogger(__name__)
from anthropic import (
    APIConnectionError,
    APIStatusError,
    InternalServerError,
    RateLimitError,
)
try:
    import git
except ImportError:
    git = None
from xuloom.core.run_result import RunResult, PersistenceError, finish_run
from xuloom.core.session_lock import SessionBusyError, SessionConflictError
from xuloom.core.reasoning import EFFORTS, configured_profile, request_options, normalize_profile
from xuloom.features.auto_commit import AutoCommitManager
from xuloom.features.browser import BrowserSessionManager
from xuloom.context.context import ContextBuilder
from xuloom.features.content_replacement import ContentReplacementManager
from xuloom.features.incremental_write_queue import IncrementalWriteQueue
from xuloom.features.artifact_tracker import ArtifactTracker
from xuloom.context.file_state_cache import FileStateCache
from xuloom.core.hooks import HookManager
from xuloom.memory.memory import MemoryCompactor
from xuloom.context.memory_files import MemoryFileManager
from xuloom.memory.session_memory import SessionMemoryManager
from xuloom.memory.extract_memories import MemoryExtractor
from xuloom.security.security_classifier import AutoSecurityClassifier
from xuloom.features.structured_output import StructuredOutputManager
from xuloom.features.plans import PlanManager, is_session_plan_file
from xuloom.security.permissions import PermissionManager
from xuloom.utils.shell_tasks import ShellTaskManager
from xuloom.context.tool_results import build_tool_result
from xuloom.core.tool_execution import execute_tool, execute_tool_batches, restore_tool_activity
from xuloom.protocols.dsml import ToolTextFilter, ToolProtocolError, decode_tool_text, parse_tool_arguments
from xuloom.features.todos import TodoManager
from xuloom.tools.goal_tool import GoalReportTool
from xuloom.tools.schedule_tool import ScheduleTool
from xuloom.features.workflow_completion import WorkflowCompletion
from xuloom.protocols.stream_watch import StreamWatch, StreamAborted, StreamInterrupted
from xuloom.utils.vcr import VCRManager
from xuloom.features.ultraplan import UltraplanManager
from xuloom.agents.team import TeamManager
from xuloom.tools.bash_tool import BashTool
from xuloom.tools.command_tool import CommandTool
from xuloom.utils.command_sessions import CommandSessions
from xuloom.tools.file_read_tool import FileReadTool
from xuloom.tools.file_edit_tool import FileEditTool
from xuloom.tools.file_write_tool import FileWriteTool
from xuloom.tools.grep_tool import GrepTool
from xuloom.tools.glob_tool import GlobTool
from xuloom.protocols.mcp_bridge import MCPBridge
from xuloom.protocols.lsp_manager import LSPManager
from xuloom.security.sandbox import SandboxManager
from xuloom.core.session import SessionManager
from xuloom.core.storage import artifact_directory, history_root
from xuloom.tools.lsp_tool import LspTool
from xuloom.tools.sandbox_tool import SandboxTool
from xuloom.tools.task_tools import (
    TaskCreateTool,
    TaskKillTool,
    TaskListTool,
    TaskReadTool,
    TaskStatusTool,
)
from xuloom.tools.web_tool import WebSearchTool, WebFetchTool
from xuloom.tools.notebook_tool import NotebookTool
from xuloom.tools.notebook_edit_tool import NotebookEditTool
from xuloom.tools.plan_tool import PlanTool
from xuloom.tools.todo_tool import TodoWriteTool
from xuloom.agents.agent_tool import AgentTool, SpawnAgentTool
from xuloom.tools.browser_tool import BrowserTool
from xuloom.tools.repl_tool import ReplTool
from xuloom.skills.skill_tool import SkillTool
from xuloom.skills.registry import SkillRegistry
from xuloom.agents.registry import AgentRegistry
from xuloom.agents.tasks import SubagentTasks
from xuloom.tools.tool_search_tool import ToolSearchTool
from xuloom.tools.team_tools import TeamCreateTool, TeamDeleteTool
from xuloom.tools.send_message_tool import SendMessageTool
from xuloom.tools.ask_user_question_tool import AskUserQuestionTool
from xuloom.tools.brief_tool import BriefTool
from xuloom.tools.plan_mode_tools import EnterPlanModeTool, ExitPlanModeTool
from xuloom.core.messages import (
    create_user_message,
    create_assistant_message,
    create_tool_result_message,
    get_msg_uuid,
    strip_internal_fields,
    rollback_incomplete_turn,
    export_messages_state,
)
from xuloom.context.attachments import AttachmentCollector
from xuloom.utils.frc import clear_old_function_results
from xuloom.features.verification import VerificationManager
import uuid


MAX_SESSION_MEMORY_IN_COMPACT = 32000


class AbortRequestedError(Exception):
    pass

class QueryEngine:
    """
    The main Agentic loop mapping the Context System and Tools to the Anthropic REST API.
    """
    def __init__(
        self,
        model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        permission_handler=None,
        thinking_config=None,
        mode_thinking_overrides=None,
        agent_id: str = None,
        parent_agent_id: str = None,
        agent_depth: int = 0,
        agent_role: str = "main",
        model_provider: str = None,
        api_base_url: str = None,
        local_tokenizer_path: str = None,
        cwd: Optional[str] = None,
        approval_policy=None,
        reasoning_effort=None,
        collaboration_mode=None,
        execution_mode=None,
        api_key=None,
        network_mode=None,
        model_contract=None,
    ):
        from xuloom.security.execution import ExecutionSandbox
        self.session_cwd = os.path.abspath(cwd or os.getcwd())
        self.execution_sandbox = ExecutionSandbox(self.session_cwd, execution_mode or os.environ.get("XULOOM_EXECUTION_MODE", "host"))
        self.collaboration_mode = collaboration_mode or os.environ.get("XULOOM_COLLABORATION_MODE", "auto")
        if reasoning_effort is not None and reasoning_effort not in EFFORTS:
            raise ValueError("Unsupported reasoning_effort")
        self.reasoning_effort = reasoning_effort
        self.model_provider = self._resolve_model_provider(model_provider, model)
        self.api_base_url = api_base_url if api_base_url is not None else os.environ.get(self.model_provider.upper() + "_BASE_URL", "")
        self._api_key = api_key if api_key is not None else os.environ.get(self.model_provider.upper() + "_API_KEY", "")
        from xuloom.core.network import model_network_mode
        self.network_mode = model_network_mode(network_mode)
        self._retired_model_clients = []
        self.local_tokenizer_path = (
            local_tokenizer_path
            or os.environ.get("XULOOM_LOCAL_TOKENIZER_PATH", "")
            or os.environ.get("XULOOM_LOCAL_TOKENIZER_MODEL", "")
        )
        self.primary_model = self._resolve_primary_model(model)
        # A resolved role binding supplies an exact account contract, including
        # explicit unknown capabilities. Do not guess another same-URL account.
        self._model_contract = copy.deepcopy(model_contract)
        profile = model_contract.get("reasoning") if model_contract is not None else configured_profile(self.model_provider, self.primary_model, self.api_base_url)
        self.reasoning_profile = normalize_profile(profile, self.model_provider) if profile is not None else None
        if reasoning_effort and self.model_provider != "openai" and self.reasoning_profile is None:
            raise ValueError("Anthropic effort requires an explicit model reasoning profile")
        if self.reasoning_profile is not None:
            request_options(self.reasoning_profile, self.model_provider, effort_override=reasoning_effort)
        self.fallback_model = self._resolve_fallback_model(fallback_model)
        self.model = self.primary_model
        self.client = self._build_model_client()
        self._thinking_config_explicit = thinking_config is not None or bool(mode_thinking_overrides)
        self.default_thinking_config = self._normalize_thinking_config(
            thinking_config if thinking_config is not None else {"type": "adaptive", "display": "summarized"}
        )
        normalized_mode_overrides = {
            "plan": {"type": "disabled"},
        }
        for mode_name, config in dict(mode_thinking_overrides or {}).items():
            normalized = self._normalize_thinking_config(config)
            if normalized is None:
                normalized_mode_overrides.pop(str(mode_name), None)
            else:
                normalized_mode_overrides[str(mode_name)] = normalized
        self.mode_thinking_overrides = normalized_mode_overrides
        self.agent_id = agent_id or str(uuid.uuid4())
        self.parent_agent_id = parent_agent_id
        self.agent_depth = agent_depth
        self.agent_role = agent_role
        self.context_builder = ContextBuilder(self.session_cwd)
        self.hook_manager = HookManager(cwd=self.session_cwd)
        self.hook_manager.execution_sandbox = self.execution_sandbox
        self.session_id = str(uuid.uuid4())
        self.compactor = MemoryCompactor(
            model=self.fallback_model or self.primary_model,
            provider=self.model_provider,
            api_base_url=self.api_base_url,
            local_tokenizer_path=self.local_tokenizer_path,
            api_key=self._api_key,
            network_mode=self.network_mode,
        )
        self.plan_manager = PlanManager(session_id=self.session_id)
        self.structured_output_manager = StructuredOutputManager()
        self.security_classifier = AutoSecurityClassifier()
        self.permission_manager = PermissionManager(
            permission_handler,
            config_path=configuration_file(self.session_cwd, "permissions.json"),
            mode_getter=self.plan_manager.get_mode,
            state_change_callback=self.persist_session_state,
            classifier_manager=self.security_classifier,
            session_id_getter=lambda: self.session_id,
            approval_policy=approval_policy,
        )
        self.todo_manager = TodoManager()
        self.subagent_registry = []
        self.in_progress_tools = {}
        self.recent_tool_activity = []
        self.goal = None
        self.scheduled_deliveries = {}
        self.loop_transition_history = []
        self.token_usage_history = []
        self.post_sampling_history = []
        self.turn_traces = []
        self.active_turn_trace = None
        self.session_token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        
        self.messages = []
        self._compact_boundary_index = 0
        self._tool_result_budget_chars = int(os.environ.get("XULOOM_TOOL_RESULT_BUDGET_CHARS", "0")) or 200000
        self.session_manager = SessionManager(cwd=self.session_cwd)
        self.last_persist_ok = True
        self.last_persist_error = ""
        self._last_reported_persist_error = ""
        self.subagent_state_lock = asyncio.Lock()
        self.abort_event = asyncio.Event()
        self.active_run_task = None
        self.active_streaming_tool_entries = []
        self.active_stream = None
        self.sandbox_manager = SandboxManager(cwd=self.session_cwd)
        runtime_root = history_root(self.session_cwd)
        self.browser_manager = BrowserSessionManager(artifact_dir=str(runtime_root / "browser"))
        self.shell_task_manager = ShellTaskManager(log_dir=str(runtime_root / "shell-tasks"))
        self.memory_file_manager = MemoryFileManager(cwd=self.session_cwd)
        self.session_memory_manager = SessionMemoryManager(
            session_id=self.session_id,
            compactor=self.compactor,
            on_update=lambda: self.persist_session_state(),
        )
        self.memory_extractor = MemoryExtractor(
            compactor=self.compactor,
            project_dir=self.session_cwd,
            on_update=lambda: self.persist_session_state(),
        )
        self.file_state_cache = FileStateCache()
        self.artifact_tracker = ArtifactTracker()
        self.incremental_write_queue = IncrementalWriteQueue()
        self.vcr_manager = VCRManager(cassette_dir=str(runtime_root / "vcr"))
        self.auto_commit_manager = AutoCommitManager(state_change_callback=self.persist_session_state)
        self.content_replacement_manager = ContentReplacementManager(base_dir=str(artifact_directory(self.session_cwd)), session_id=self.session_id)
        self.ultraplan_manager = UltraplanManager()
        self.team_manager = TeamManager()
        self.attachment_collector = AttachmentCollector()
        self.verification_manager = VerificationManager()
        self.coordinator_mode = False

        all_tools = {
            "bash_tool": BashTool(),
            "command_tool": CommandTool(),
            "file_read_tool": FileReadTool(),
            "file_edit_tool": FileEditTool(),
            "file_write_tool": FileWriteTool(),
            "grep_tool": GrepTool(),
            "glob_tool": GlobTool(),
            "sandbox_tool": SandboxTool(),
            "task_create_tool": TaskCreateTool(),
            "task_status_tool": TaskStatusTool(),
            "task_read_tool": TaskReadTool(),
            "task_kill_tool": TaskKillTool(),
            "task_list_tool": TaskListTool(),
            "web_search_tool": WebSearchTool(),
            "web_fetch_tool": WebFetchTool(),
            "notebook_tool": NotebookTool(),
            "notebook_edit_tool": NotebookEditTool(),
            "plan_tool": PlanTool(),
            "todo_write_tool": TodoWriteTool(),
            "agent_tool": AgentTool(),
            "spawn_agent": SpawnAgentTool(),
            "browser_tool": BrowserTool(),
            "repl_tool": ReplTool(),
            "skill_tool": SkillTool(),
            "tool_search_tool": ToolSearchTool(),
            "goal_report_tool": GoalReportTool(),
            "schedule_tool": ScheduleTool(),
            "team_create_tool": TeamCreateTool(),
            "team_delete_tool": TeamDeleteTool(),
            "send_message_tool": SendMessageTool(),
            "ask_user_question_tool": AskUserQuestionTool(),
            "brief_tool": BriefTool(),
            "enter_plan_mode": EnterPlanModeTool(),
            "exit_plan_mode": ExitPlanModeTool(),
        }
        from xuloom.tools.media_tool import MediaTool
        all_tools["media_tool"] = MediaTool()
        latent_tool_names = {
            "media_tool",
            "goal_report_tool",
            "schedule_tool",
            "browser_tool",
            "notebook_tool",
            "notebook_edit_tool", "repl_tool", "sandbox_tool", "brief_tool",
            "task_create_tool", "task_status_tool", "task_read_tool", "task_kill_tool", "task_list_tool",
            "team_create_tool", "team_delete_tool", "send_message_tool",
        }
        self.available_tools = {
            name: tool for name, tool in all_tools.items() if name not in latent_tool_names
        }
        self.latent_tools = {
            name: tool for name, tool in all_tools.items() if name in latent_tool_names
        }
        
        # Share state across instances for locks and cross-tool persistence
        self.tool_context = {
            "execution_sandbox": self.execution_sandbox,
            "workflow_completion": WorkflowCompletion(),
            "skill_registry": SkillRegistry(self.session_cwd),
            "agent_registry": AgentRegistry(self.session_cwd),
            "subagent_tasks": SubagentTasks(),
            "session_cwd": self.session_cwd,
            "read_file_state": {},
            "file_state_cache": self.file_state_cache,
            "artifact_tracker": self.artifact_tracker,
            "verification_manager": self.verification_manager,
            "incremental_write_queue": self.incremental_write_queue,
            "auto_commit_manager": self.auto_commit_manager,
            "content_replacement_manager": self.content_replacement_manager,
            "sandbox_manager": self.sandbox_manager,
            "engine_available_tools": self.available_tools,
            "latent_tools_registry": self.latent_tools,
            "activate_tools": self.activate_tools,
            "permission_manager": self.permission_manager,
            "permission_handler": permission_handler,
            "plan_manager": self.plan_manager,
            "structured_output_manager": self.structured_output_manager,
            "security_classifier": self.security_classifier,
            "todo_manager": self.todo_manager,
            "session_manager": self.session_manager,
            "hook_manager": self.hook_manager,
            "subagent_registry": self.subagent_registry,
            "register_subagent_record": self.register_subagent_record,
            "subagent_state_lock": self.subagent_state_lock,
            "team_manager": self.team_manager,
            "ultraplan_manager": self.ultraplan_manager,
            "browser_manager": self.browser_manager,
            "shell_task_manager": self.shell_task_manager,
            "command_sessions": self._new_command_sessions(self.session_id),
            "in_progress_tools": self.in_progress_tools,
            "recent_tool_activity": self.recent_tool_activity,
            "loop_transition_history": self.loop_transition_history,
            "token_usage_history": self.token_usage_history,
            "post_sampling_history": self.post_sampling_history,
            "session_token_usage": self.session_token_usage,
            "thinking_config": self.default_thinking_config,
            "thinking_config_explicit": self._thinking_config_explicit,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_profile": copy.deepcopy(self.reasoning_profile),
            "mode_thinking_overrides": self.mode_thinking_overrides,
            "model_provider": self.model_provider,
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "api_base_url": self.api_base_url,
            "local_tokenizer_path": self.local_tokenizer_path,
            "memory_files": self.memory_file_manager.export_state(),
            "file_state_cache_state": self.file_state_cache.export_state(),
            "artifact_tracker_state": self.artifact_tracker.export_state(),
            "incremental_write_queue_state": self.incremental_write_queue.export_state(),
            "auto_commit_state": self.auto_commit_manager.export_state(),
            "content_replacement_state": self.content_replacement_manager.export_state(),
            "structured_output_state": self.structured_output_manager.export_state(),
            "security_classifier_state": self.security_classifier.export_state(),
            "vcr_state": self.vcr_manager.export_state(),
            "attachment_collector": self.attachment_collector,
            "abort_event": self.abort_event,
            "messages": self.messages,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "parent_agent_id": self.parent_agent_id,
            "agent_depth": self.agent_depth,
            "agent_role": self.agent_role,
        }
        self.set_collaboration_mode(self.collaboration_mode, persist=False)
        self._sync_model_runtime_context()
        from xuloom.features.schedules import add as add_schedule
        self.tool_context["schedule_add"] = lambda prompt, **options: add_schedule(self, prompt, **options)
        for t in self.available_tools.values():
            t.context = self.tool_context
        for t in self.latent_tools.values():
            t.context = self.tool_context

    def set_permission_handler(self, permission_handler):
        self.permission_manager.set_prompt_handler(permission_handler)
        self.tool_context["permission_handler"] = permission_handler

    def switch_model_runtime(
        self,
        *,
        provider: str = None,
        model: str = None,
        api_base_url: str = None,
        api_key: str = None,
        network_mode: str = None,
        persist: bool = True,
    ) -> dict:
        """
        Hot-swap the model provider, model name, API base URL, and/or API key at runtime.
        Returns a dict with the new effective configuration.
        """
        self._assert_open()
        workers = self.tool_context.get("subagent_tasks")
        if workers and any(not task.done() for task in workers.tasks.values()):
            raise ValueError("Finish or cancel owned workers before changing the model connection")
        from xuloom.core.network import model_network_mode, create_model_client
        next_network = model_network_mode(network_mode if network_mode is not None else self.network_mode)
        next_provider = self._resolve_model_provider(provider or self.model_provider, model or self.primary_model)
        next_model = model if model is not None else self.primary_model
        next_base = api_base_url if api_base_url is not None else (self.api_base_url if next_provider == self.model_provider else os.environ.get(next_provider.upper() + "_BASE_URL", ""))
        next_key = api_key if api_key is not None else (self._api_key if next_provider == self.model_provider else os.environ.get(next_provider.upper() + "_API_KEY", ""))
        next_fallback = self._resolve_fallback_model(None, provider=next_provider, primary_model=next_model)
        next_profile = configured_profile(next_provider, next_model, next_base)
        next_client = create_model_client(provider=next_provider, model=next_model, api_base_url=next_base, api_key=next_key, network_mode=next_network)
        try:
            next_compactor = MemoryCompactor(model=next_fallback or next_model, provider=next_provider,
                api_base_url=next_base, local_tokenizer_path=self.local_tokenizer_path, api_key=next_key, network_mode=next_network)
        except BaseException:
            self._retired_model_clients.append(next_client)
            raise
        if next_provider != self.model_provider or next_model != self.primary_model or next_profile != self.reasoning_profile:
            self._strip_thinking_signatures_from_history()
        self._retired_model_clients = [client for client in self._retired_model_clients if not client.is_closed]
        self._retired_model_clients.extend(client for client in (self.client, self.compactor.client) if client and not client.is_closed)
        self.model_provider, self.primary_model, self.model = next_provider, next_model, next_model
        self.api_base_url, self._api_key, self.network_mode = next_base, next_key, next_network
        self._model_contract = None
        self.fallback_model = next_fallback
        self.reasoning_profile = next_profile
        self.reasoning_effort = None  # Per-model controls must not leak from the previous model.
        self._thinking_config_explicit = False
        for manager in (self.session_memory_manager, self.memory_extractor):
            manager.cancel_pending()
        self.client, self.compactor = next_client, next_compactor
        self._sync_model_runtime_context()
        if persist:
            self.persist_session_state()

        return {
            "provider": self.model_provider,
            "model": self.primary_model,
            "api_base_url": self.api_base_url or "(default)",
            "has_api_key": bool(self._api_key),
        }

    def _sync_model_runtime_context(self):
        self.tool_context.pop("request_model", None)
        for name in ("session_memory_manager", "memory_extractor"):
            manager = getattr(self, name, None)
            if manager is not None:
                manager.compactor = self.compactor
        self.tool_context["model_provider"] = self.model_provider
        self.tool_context["primary_model"] = self.primary_model
        self.tool_context["fallback_model"] = self.fallback_model
        self.tool_context["api_base_url"] = self.api_base_url
        self.tool_context["local_tokenizer_path"] = self.local_tokenizer_path
        self.tool_context["reasoning_effort"] = self.reasoning_effort
        self.tool_context["reasoning_profile"] = copy.deepcopy(self.reasoning_profile)
        self.tool_context["thinking_config_explicit"] = self._thinking_config_explicit
        # Internal callable only: never place credentials into serializable
        # tool descriptions, worker records or checkpoint metadata.
        self.tool_context["model_client_options"] = lambda: {"api_key": self._api_key, "network_mode": self.network_mode}
        self.tool_context["model_input_contract"] = self._input_contract

    def _resolve_model_provider(self, provider: str = None, model: str = None) -> str:
        resolved = str(
            provider
            or os.environ.get("XULOOM_MODEL_PROVIDER", "")
            or ""
        ).strip().lower()
        if resolved in {"anthropic", "openai"}:
            return resolved
        model_name = str(model or "").strip().lower()
        if model_name.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            return "openai"
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
            return "anthropic"
        return ""

    def _resolve_primary_model(self, model: Optional[str]) -> str:
        candidate = str(model or "").strip()
        if self.model_provider == "openai":
            if not candidate:
                return os.environ.get("OPENAI_MODEL", "") or os.environ.get("XULOOM_OPENAI_MODEL", "") or ""
            return candidate
        if not candidate:
            return os.environ.get("ANTHROPIC_MODEL", "") or os.environ.get("XULOOM_ANTHROPIC_MODEL", "") or ""
        return candidate

    def _resolve_fallback_model(self, fallback_model: Optional[str], *, provider=None, primary_model=None) -> str:
        candidate = str(fallback_model or "").strip()
        provider = provider if provider is not None else self.model_provider
        primary_model = primary_model if primary_model is not None else self.primary_model
        if provider == "openai":
            if not candidate:
                return os.environ.get("OPENAI_FALLBACK_MODEL", "") or os.environ.get("XULOOM_OPENAI_FALLBACK_MODEL", "") or primary_model
            return candidate
        if not candidate:
            return os.environ.get("ANTHROPIC_FALLBACK_MODEL", "") or primary_model
        return candidate

    def _build_model_client(self):
        from xuloom.core.network import create_model_client
        return create_model_client(provider=self.model_provider, model=self.primary_model,
            api_base_url=self.api_base_url, api_key=self._api_key, network_mode=self.network_mode)

    def _input_contract(self, model):
        from xuloom.core.model_inputs import configured_inputs
        if self._model_contract is not None and model == self.primary_model:
            return self._model_contract.get("inputs")
        return configured_inputs(self.model_provider, model, self.api_base_url)

    @property
    def is_configured(self) -> bool:
        """Check if minimum configuration (provider + model) is present."""
        return bool(self.model_provider and self.primary_model)

    def _openai_supports_tools(self) -> bool:
        return self.model_provider == "openai"

    def _openai_supports_reasoning(self) -> bool:
        return False

    def _map_openai_finish_reason(self, finish_reason: str) -> str:
        mapped = str(finish_reason or "").strip().lower()
        if mapped == "tool_calls":
            return "tool_use"
        if mapped == "length":
            return "max_tokens"
        if mapped in {"stop", "content_filter"}:
            return "end_turn"
        return mapped or "end_turn"

    def _make_text_block(self, text: str):
        return SimpleNamespace(type="text", text=text)

    def _make_tool_use_block(self, *, tool_id: str, name: str, input_payload: dict):
        return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_payload or {})

    def _make_usage_block(self, *, input_tokens: int = 0, output_tokens: int = 0):
        return SimpleNamespace(
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    def _assistant_text_from_blocks(self, blocks: list) -> str:
        return "".join(
            str(block.get("text", ""))
            for block in blocks or []
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def _openai_tool_definition(self, tool_def: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": tool_def.get("name", ""),
                "description": tool_def.get("description", ""),
                "parameters": copy.deepcopy(tool_def.get("input_schema", {}) or {"type": "object", "properties": {}}),
            },
        }

    def _clean_roles_for_openai_api(self, messages, system_prompt: str):
        def parts(blocks):
            result = []
            for block in blocks:
                if block.get("type") == "text":
                    result.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        result.append({"type": "image_url", "image_url": {"url": "data:" + source["media_type"] + ";base64," + source["data"]}})
                elif block.get("type") == "document":
                    result.append({"type": "text", "text": "[PDF document: use file_read_tool with its attached local path to extract text.]"})
            return result

        def user_message(blocks):
            values = parts(blocks)
            return {"role": "user", "content": values if any(item["type"] == "image_url" for item in values) else "".join(item.get("text", "") for item in values)}

        normalized_messages = self._clean_roles_for_api(messages)
        rendered = []
        if system_prompt:
            rendered.append({"role": "system", "content": system_prompt})

        for msg in normalized_messages:
            role = msg.get("role")
            content = list(msg.get("content", []) or [])
            if role not in {"user", "assistant"}:
                continue

            if role == "assistant":
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                    elif block_type == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}) or {}, ensure_ascii=False),
                            },
                        })
                if text_parts or tool_calls:
                    rendered.append({
                        "role": "assistant",
                        "content": "".join(text_parts) if text_parts else None,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    })
                continue

            user_parts = []
            tool_images = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    user_parts.append(block)
                elif block_type in {"image", "document"}:
                    user_parts.append(block)
                elif block_type == "tool_result":
                    if user_parts:
                        rendered.append(user_message(user_parts))
                        user_parts = []
                    value = block.get("content", "") or ""
                    if isinstance(value, list):
                        tool_images.extend(item for item in value if item.get("type") in {"image", "document"})
                        value = "".join(item.get("text", "") for item in value if item.get("type") == "text")
                    rendered.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(value),
                    })
            if tool_images or user_parts:
                rendered.append(user_message(tool_images + user_parts))

        return rendered

    def get_pending_auto_commit_proposal(self):
        return copy.deepcopy(self.auto_commit_manager.pending_proposal)

    def _get_git_repo(self, start_path: str = None):
        if git is None:
            return None
        try:
            return git.Repo(start_path or self.session_cwd, search_parent_directories=True)
        except Exception:
            return None

    def _safe_branch_name(self, repo) -> str:
        try:
            return repo.active_branch.name
        except Exception:
            return ""

    def _repo_relative_path(self, repo, abs_path: str) -> str:
        try:
            rel = os.path.relpath(os.path.abspath(abs_path), repo.working_tree_dir)
        except Exception:
            return ""
        if rel.startswith(".."):
            return ""
        return rel.replace("\\", "/")

    def _status_paths_for_repo(self, repo) -> set:
        if repo is None:
            return set()
        try:
            rendered = repo.git.status("--porcelain")
        except Exception:
            return set()
        paths = set()
        for line in rendered.splitlines():
            entry = line[3:].strip()
            if not entry:
                continue
            if "->" in entry:
                entry = entry.split("->", 1)[1].strip()
            abs_path = os.path.abspath(os.path.join(repo.working_tree_dir, entry))
            paths.add(abs_path)
        return paths






    async def resolve_auto_commit_proposal(
        self,
        proposal_id: str,
        decision: str = "skip",
        message_override: str = None,
    ) -> dict:
        proposal = self.auto_commit_manager.pending_proposal
        if not proposal or proposal.get("proposal_id") != proposal_id:
            return build_tool_result(
                ok=False,
                content="No matching auto-commit proposal is pending.",
                metadata={"proposal_id": proposal_id},
                is_error=True,
            )

        normalized = str(decision or "skip").strip().lower()
        if normalized not in {"commit", "approve", "yes", "y", "skip", "no", "n"}:
            normalized = "skip"
        if normalized in {"skip", "no", "n"}:
            resolved = await self.auto_commit_manager.resolve_pending(
                status="skipped",
                metadata={"decision": normalized},
            )
            self._sync_message_context()
            self.persist_session_state()
            return build_tool_result(
                ok=True,
                content="Auto-commit proposal skipped.",
                metadata={"proposal": resolved},
            )

        repo = self._get_git_repo(proposal.get("repo_root"))
        if repo is None:
            resolved = await self.auto_commit_manager.resolve_pending(
                status="failed",
                metadata={"reason": "git repo unavailable"},
            )
            self._sync_message_context()
            self.persist_session_state()
            return build_tool_result(
                ok=False,
                content="Unable to resolve git repository for auto-commit.",
                metadata={"proposal": resolved},
                is_error=True,
            )

        commit_message = str(message_override or proposal.get("message") or "").strip()
        files = proposal.get("files", []) or []
        relpaths = [item.get("relative_path") for item in files if item.get("relative_path")]
        if not relpaths:
            resolved = await self.auto_commit_manager.resolve_pending(
                status="failed",
                metadata={"reason": "no eligible files"},
            )
            self._sync_message_context()
            self.persist_session_state()
            return build_tool_result(
                ok=False,
                content="Auto-commit proposal has no eligible files to commit.",
                metadata={"proposal": resolved},
                is_error=True,
            )

        head_before = ""
        try:
            head_before = repo.head.commit.hexsha
        except Exception:
            head_before = ""

        try:
            repo.index.add(relpaths)
            commit_obj = repo.index.commit(commit_message)
            commit_hash = str(getattr(commit_obj, "hexsha", "") or "")
            self.artifact_tracker.record_commit(
                cwd=proposal.get("repo_root") or repo.working_tree_dir,
                command="auto_commit_orchestrator",
                success=True,
                status="created",
                commit_hash=commit_hash,
                message_preview=commit_message.splitlines()[0][:240] if commit_message else "",
                head_before=head_before,
                head_after=commit_hash,
                branch=proposal.get("branch") or self._safe_branch_name(repo),
                repo_root=repo.working_tree_dir,
                author_name=getattr(getattr(commit_obj, "author", None), "name", "") or "",
                author_email=getattr(getattr(commit_obj, "author", None), "email", "") or "",
                tool_name="auto_commit_orchestrator",
                agent_role=self.agent_role,
                parent_agent_id=self.parent_agent_id,
                agent_depth=self.agent_depth,
                exit_code=0,
                agent_id=self.agent_id,
                session_id=self.session_id,
            )
            resolved = await self.auto_commit_manager.resolve_pending(
                status="committed",
                metadata={"commit_hash": commit_hash, "decision": normalized},
            )
            self._sync_message_context()
            self.persist_session_state()
            return build_tool_result(
                ok=True,
                content=f"Auto-commit created: {commit_hash[:12] if commit_hash else '<unknown>'}",
                metadata={"proposal": resolved, "commit_hash": commit_hash, "files": relpaths},
            )
        except Exception as e:
            self.artifact_tracker.record_commit(
                cwd=proposal.get("repo_root") or repo.working_tree_dir,
                command="auto_commit_orchestrator",
                success=False,
                status="failed",
                commit_hash="",
                message_preview=commit_message.splitlines()[0][:240] if commit_message else "",
                head_before=head_before,
                head_after="",
                branch=proposal.get("branch") or self._safe_branch_name(repo),
                repo_root=repo.working_tree_dir,
                tool_name="auto_commit_orchestrator",
                agent_role=self.agent_role,
                parent_agent_id=self.parent_agent_id,
                agent_depth=self.agent_depth,
                exit_code=None,
                agent_id=self.agent_id,
                session_id=self.session_id,
            )
            resolved = await self.auto_commit_manager.resolve_pending(
                status="failed",
                metadata={"error": str(e), "decision": normalized},
            )
            self._sync_message_context()
            self.persist_session_state()
            return build_tool_result(
                ok=False,
                content=f"Auto-commit failed: {str(e)}",
                metadata={"proposal": resolved},
                is_error=True,
            )

    def has_active_turn(self) -> bool:
        return self.active_run_task is not None and not self.active_run_task.done()

    async def request_abort(self) -> bool:
        self.abort_event.set()
        memory_jobs = [manager.cancel_pending() for manager in (self.session_memory_manager, self.memory_extractor)]
        await asyncio.gather(*(task for task in memory_jobs if task is not None), return_exceptions=True)
        workers = self.tool_context.get("subagent_tasks")
        had_workers = bool(workers and any(not task.done() for task in workers.tasks.values()))
        if workers:
            await workers.close()
        await self.shell_task_manager.close(owner=self.agent_id if self.agent_depth else None)
        if not self.has_active_turn():
            return had_workers

        self.abort_event.set()

        if self.active_stream is not None:
            try:
                await self.active_stream.close()
            except Exception:
                pass

        if self.active_streaming_tool_entries:
            await self._cancel_streaming_tool_tasks(self.active_streaming_tool_entries)

        try:
            await asyncio.wait_for(asyncio.shield(self.active_run_task), timeout=1.5)
        except asyncio.TimeoutError:
            self.active_run_task.cancel()
            await asyncio.gather(self.active_run_task, return_exceptions=True)
        except Exception:
            pass

        return True

    def activate_tools(self, tool_names):
        activated = []
        for name in tool_names or []:
            if self.execution_sandbox.enabled:
                from xuloom.security.execution import FILE_TOOLS, CONTROL_TOOLS, PROCESS_TOOLS
                if name not in FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS:
                    continue
            tool = self.latent_tools.pop(name, None)
            if not tool:
                continue
            tool.context = self.tool_context
            self.available_tools[name] = tool
            activated.append(name)

        self.tool_context["engine_available_tools"] = self.available_tools
        self.tool_context["latent_tools_registry"] = self.latent_tools
        return activated

    def register_mcp_tools(self, tools):
        if set(tools).intersection(self.available_tools.keys() | self.latent_tools.keys()):
            raise ValueError("MCP tool conflicts with an existing tool")
        for name, tool in tools.items():
            tool.context = self.tool_context
            self.latent_tools[name] = tool
        return set(tools)


    def _apply_frc_if_needed(self):
        """Apply Function Result Clearing to keep context window manageable."""
        if not hasattr(self, "_frc_cleared_ids"):
            self._frc_cleared_ids = set()
        if len(self.messages) < 10:
            return
        self.messages, num_cleared, self._frc_cleared_ids = clear_old_function_results(
            self.messages,
            preserve_recent_results=12,
            min_content_chars=800,
            cleared_ids=self._frc_cleared_ids,
        )

    # ── Compact Boundary Slicing ──────────────────────────────────────
    def _find_last_compact_boundary_index(self) -> int:
        """Find the index of the last compaction boundary message."""
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            content = msg.get("content", "")
            if isinstance(content, str) and "[CompactionBoundary:" in content:
                return i
        return 0

    def _get_messages_after_compact_boundary(self) -> list:
        """Return only messages from the last compact boundary onward for API calls.

        This avoids sending both the summary and the original messages that
        were summarized, which wastes tokens and degrades compact effectiveness.
        """
        boundary = self._compact_boundary_index
        if boundary <= 0 or boundary >= len(self.messages):
            return self.messages
        return self.messages[boundary:]

    def _update_compact_boundary(self):
        """Refresh the boundary index after compaction modifies self.messages."""
        self._compact_boundary_index = self._find_last_compact_boundary_index()

    # ── Tool Result Budget Pool ───────────────────────────────────────
    def _apply_tool_result_budget(self, messages: list) -> list:
        """Delegate to ContentReplacementManager for per-message budget enforcement.

        Each user message's tool_result blocks are evaluated independently.
        When the aggregate size exceeds the budget, the largest fresh results
        are spilled to disk and replaced with a preview.  Already-seen results
        keep their prior decision for wire-payload stability.
        """
        self.content_replacement_manager.enforce_budget(
            messages, budget=self._tool_result_budget_chars,
        )
        return messages

    def _is_native_anthropic(self) -> bool:
        """True only when talking to Anthropic's own API, not a third-party proxy."""
        base = (self.api_base_url or os.environ.get("ANTHROPIC_BASE_URL", "")).lower()
        if not base or "anthropic.com" in base:
            return True
        return False

    def _build_cached_system_blocks(self, system_prompt: str) -> list:
        """
        Split system prompt into cached blocks for Anthropic's prompt caching.
        
        The static prefix (identity, rules, tool guidance) is marked with
        cache_control so it's reused across turns. The dynamic suffix
        (environment, git, todos, memory) changes each turn.
        Only used for native Anthropic API; third-party proxies (MiniMax etc.)
        get a plain string to avoid incompatible parameters.
        """
        if self.model_provider != "anthropic" or not self._is_native_anthropic():
            return system_prompt

        # Cache the actual request prompt, retaining model/fallback metadata and role addons.
        static, separator, dynamic = system_prompt.partition("\n\n# Environment\n")
        if not separator:
            return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        dynamic = "# Environment\n" + dynamic
        blocks = [
            {
                "type": "text",
                "text": static,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": dynamic,
            },
        ]
        return blocks

    def _apply_message_cache_control(self, messages: list) -> list:
        """Add cache_control breakpoint to maximize Anthropic prefix-cache hits.

        Mirrors Claude Code's single-breakpoint strategy: only the last 1-2
        messages get ``cache_control: {"type": "ephemeral"}``.  Anthropic's
        cache works by prefix matching — tagging every message creates many
        short cache segments with poor hit rates.  A single breakpoint near the
        end lets the entire conversation prefix be cached as one segment.
        """
        if not messages:
            return messages

        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
            elif isinstance(content, str):
                pass

        marker_indices = []
        n = len(messages)
        if n >= 2:
            marker_indices = [n - 2, n - 1]
        elif n == 1:
            marker_indices = [0]

        for idx in marker_indices:
            msg = messages[idx]
            content = msg.get("content")
            if isinstance(content, str) and content:
                msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = {"type": "ephemeral"}

        return messages

    def _build_mcp_instructions(self) -> str:
        """Collect instructions from connected MCP servers for system prompt injection."""
        if not hasattr(self, "mcp_bridge") or self.mcp_bridge is None:
            return ""
        sessions = getattr(self.mcp_bridge, "sessions", {})
        if not sessions:
            return ""
        instruction_blocks = []
        for server_name, session_info in sessions.items():
            instructions = ""
            if isinstance(session_info, dict):
                instructions = session_info.get("instructions", "")
            elif hasattr(session_info, "instructions"):
                instructions = getattr(session_info, "instructions", "")
            if instructions:
                instruction_blocks.append(f"## {server_name}\n{instructions}")
        if not instruction_blocks:
            return ""
        return (
            "# MCP Server Instructions\n\n"
            "The following MCP servers have provided instructions for how to use their tools:\n\n"
            + "\n\n".join(instruction_blocks)
        )

    def _inject_turn_attachments(self, turn_count: int):
        """Collect and inject contextual attachment messages before each turn."""
        try:
            lsp_diags = ""
            if hasattr(self, "lsp_manager"):
                lsp_diags = self.lsp_manager.consume_diagnostics() or ""
        except Exception:
            lsp_diags = ""

        try:
            attachment_messages = self.attachment_collector.collect_attachments(
                cwd=self.session_cwd,
                plan_mode=self.plan_manager.get_mode(),
                plan_content=self.plan_manager.get_plan(),
                plan_file_path=self.plan_manager.plan_file_path,
                plan_exists=self.plan_manager.plan_exists,
                todo_summary=self.todo_manager.render_prompt_summary(),
                todo_count=len(self.todo_manager.list_items()),
                active_tools=list(self.available_tools.keys()),
                changed_files=[],
                lsp_diagnostics=lsp_diags,
                turn_count=turn_count,
                recent_tool_names=self.attachment_collector._recent_tool_names,
            )
            for att_msg in attachment_messages:
                self.messages.append(att_msg)
            if attachment_messages:
                self._sync_message_context()
        except Exception as exc:
            _logger.warning("Failed to inject turn attachments (turn=%d): %s", turn_count, exc, exc_info=True)

    def set_collaboration_mode(self, mode, *, persist=True):
        from xuloom.agents.collaboration import policy, new_run
        active = getattr(self, "active_run_task", None)
        workers = self.tool_context.get("subagent_tasks")
        if (active and not active.done()) or (workers and any(not task.done() for task in workers.tasks.values())):
            raise ValueError("请先停止或等待当前任务，再切换协作档位")
        profile = policy(mode, self.tool_context["skill_registry"].settings())
        if persist:
            self.require_writer()
        previous = (self.collaboration_mode, self.tool_context.get("collaboration"), self.tool_context.get("collaboration_run"))
        self.collaboration_mode = mode
        self.tool_context["collaboration"] = profile
        self.tool_context["collaboration_run"] = new_run(profile)
        if persist and not self.persist_session_state():
            self.collaboration_mode, self.tool_context["collaboration"], self.tool_context["collaboration_run"] = previous
            raise OSError("协作档位保存失败")
        return copy.deepcopy(profile)

    def _collaboration_instructions(self) -> str:
        from xuloom.agents.collaboration import instructions
        return instructions(self.tool_context["collaboration"])

    def _build_tool_prompt_summary(self) -> str:
        policy = self.permission_manager.approval_policy.mode
        sections = ["# Tool approval policy\n" + (
            "Full access: tool execution is authorized for this conversation. Do not ask for tool approval. "
            "Agent role and Plan mode tool limits still apply. Ask questions only for missing task information."
            if policy == "full" else
            "Request approval: the tool permission system will request user confirmation when needed. "
            "Do not use the question tool to duplicate tool approval requests.")]
        if self.execution_sandbox.enabled:
            sections.append("System workspace isolation: file tools, command_tool and repl_tool run in restricted OS processes. "
                "Network access and unconfined MCP/browser/LSP/legacy shell tools are blocked. Do not attempt host fallbacks. "
                "Only the authorized working directory is writable; runtime libraries are read-only. Approval cannot bypass isolation.")
        if self.tool_context.get("agent_system_prompt"):
            sections.append("# Assigned agent role\n" + self.tool_context["agent_system_prompt"])
        if self.tool_context.get("workspace_moves"):
            sections.append("# Conversation working directory\nThis conversation has moved between projects. "
                            "Its current working directory is " + self.session_cwd + ". "
                            "Historical paths and project instructions may belong to earlier locations. "
                            "Use the current directory and its project instructions for new tasks; do not reuse old project paths by default.")
        for name, tool in self.available_tools.items():
            prompt_builder = getattr(tool, "prompt", None)
            if not callable(prompt_builder):
                continue
            content = str(prompt_builder() or "").strip()
            if not content:
                continue
            sections.append(f"[{name}] {content}")
        return "\n".join(sections)

    def set_mode(self, mode: str) -> str:
        return self.plan_manager.set_mode(mode)

    def get_mode(self) -> str:
        return self.plan_manager.get_mode()

    # --- Coordinator mode ---

    COORDINATOR_ALLOWED_TOOLS = {
        "agent_tool", "spawn_agent", "send_message_tool", "team_create_tool",
        "team_delete_tool", "plan_tool", "todo_write_tool",
        "file_read_tool", "grep_tool", "glob_tool",
    }

    PLAN_MODE_ALLOWED_TOOLS = {
        "file_read_tool", "grep_tool", "glob_tool",
        "plan_tool", "todo_write_tool", "agent_tool", "spawn_agent",
        "web_search_tool", "web_fetch_tool",
        "lsp_tool", "tool_search_tool",
    }

    PLAN_MODE_BLOCKED_TOOLS = {
        "bash_tool", "repl_tool",
        "notebook_tool", "notebook_edit_tool", "sandbox_tool",
        "task_create_tool", "task_kill_tool",
    }

    def enable_coordinator_mode(self):
        self.activate_tools(["team_create_tool", "team_delete_tool", "send_message_tool"])
        self.coordinator_mode = True

    def disable_coordinator_mode(self):
        self.coordinator_mode = False

    def _get_effective_tools(self) -> dict:
        """Return tool set filtered by current mode (plan / coordinator)."""
        tools = self.available_tools
        if self.execution_sandbox.enabled:
            from xuloom.security.execution import FILE_TOOLS, CONTROL_TOOLS, PROCESS_TOOLS
            tools = {name: tool for name, tool in tools.items() if name in FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS}
        if self.coordinator_mode:
            return {
                name: tool for name, tool in tools.items()
                if name in self.COORDINATOR_ALLOWED_TOOLS
            }
        if self.plan_manager.get_mode() == "plan":
            return {
                name: tool for name, tool in tools.items()
                if name not in self.PLAN_MODE_BLOCKED_TOOLS
            }
        return tools

    def _get_coordinator_system_prompt_addon(self) -> str:
        if not self.coordinator_mode:
            return ""
        return (
            "\n\n--- COORDINATOR MODE ACTIVE ---\n"
            "You are operating as a **coordinator agent**. Your role:\n"
            "1. Break complex tasks into subtasks and delegate to workers via agent_tool.\n"
            "2. Use send_message_tool to send follow-up instructions to running workers.\n"
            "3. Track progress with todo_write_tool and plan_tool.\n"
            "4. You should NOT directly modify code/files — delegate that to workers.\n"
            "5. Synthesize worker results into a coherent final answer.\n"
            "Available worker-facing tools: agent_tool, send_message_tool\n"
            "Available planning tools: plan_tool, todo_write_tool\n"
            "Available read-only tools: file_read_tool, grep_tool, glob_tool\n"
        )

    def get_team_summary(self) -> str:
        return self.team_manager.get_summary()

    # --- Ultraplan ---

    async def _run_ultraplan_worker(self, description, seed_plan=""):
        receipt = await self.agents("spawn", subagent_type="plan", description="Deep planning",
            prompt="Develop a complete plan for the following task. Return the full plan as your final answer; do not write files. "
                   "Include constraints, dependencies, risks and acceptance criteria appropriate to the task.\nTask:\n" + description
                   + ("\nExisting draft:\n" + seed_plan if seed_plan else ""))
        if not receipt["ok"]:
            raise RuntimeError(receipt["error"]["message"])
        row = receipt["data"]["agents"][0]
        if row.get("status") != "completed":
            raise RuntimeError("Planning execution failed: " + str(row.get("status")) + ": " + str(row.get("result", "")))
        return row["result"]

    async def launch_ultraplan(self, description: str, seed_plan: str = "", on_phase_change=None) -> dict:
        result = await self.ultraplan_manager.launch(
            description=description,
            runner=self._run_ultraplan_worker,
            seed_plan=seed_plan,
            on_phase_change=on_phase_change,
        )
        if result.get("ok") and result.get("plan"):
            self.plan_manager.write_plan(result["plan"])
            self.set_mode("normal")
            self.persist_session_state()
        return result

    def get_ultraplan_summary(self) -> str:
        return self.ultraplan_manager.get_status_summary()

    def _normalize_thinking_config(self, config):
        if config is None or config is False or config == "disabled":
            return {"type": "disabled"}
        if config == "adaptive":
            return {"type": "adaptive", "display": "summarized"}
        if config == "enabled":
            return {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}
        if not isinstance(config, dict):
            return None

        thinking_type = str(config.get("type", "")).strip().lower()
        display = str(config.get("display", "summarized")).strip().lower()
        if display not in {"summarized", "omitted"}:
            display = "summarized"

        if thinking_type == "disabled":
            return {"type": "disabled"}
        if thinking_type == "adaptive":
            return {"type": "adaptive", "display": display}
        if thinking_type == "enabled":
            budget_tokens = config.get("budget_tokens", 2048)
            try:
                budget_tokens = int(budget_tokens)
            except (TypeError, ValueError):
                budget_tokens = 2048
            budget_tokens = max(1024, budget_tokens)
            return {
                "type": "enabled",
                "budget_tokens": budget_tokens,
                "display": display,
            }
        return None

    def set_reasoning_options(self, *, thinking=None, effort=None):
        if self.has_active_turn() or any(not task.done() for task in self.tool_context["subagent_tasks"].tasks.values()):
            raise SessionBusyError("请先停止或等待任务，再切换推理设置")
        if self.reasoning_profile is None:
            if thinking is not None:
                raise ValueError("请先在模型设置中声明 Thinking 参数格式，不能根据模型别名猜测")
            if effort not in (None, "default") and effort not in EFFORTS:
                raise ValueError("Unsupported reasoning effort")
            if effort not in (None, "default") and self.model_provider != "openai":
                raise ValueError("请先声明 Anthropic 模型的 effort 能力")
            self.reasoning_effort = None if effort in (None, "default") else effort
        else:
            profile = {**self.reasoning_profile, **({"thinking": thinking} if thinking is not None else {}),
                **({"effort": effort} if effort is not None else {})}
            profile = normalize_profile(profile, self.model_provider)
            if profile != self.reasoning_profile:
                self._strip_thinking_signatures_from_history()
            self.reasoning_profile, self.reasoning_effort = profile, None
        self._sync_model_runtime_context()
        return copy.deepcopy(self.reasoning_profile or {"effort": self.reasoning_effort or "default"})

    def _thinking_config_to_text(self, config) -> str:
        normalized = self._normalize_thinking_config(config)
        if not normalized:
            return "disabled"
        if normalized["type"] == "enabled":
            return (
                f"enabled (budget_tokens={normalized.get('budget_tokens')}, "
                f"display={normalized.get('display', 'summarized')})"
            )
        if normalized["type"] == "adaptive":
            return f"adaptive (display={normalized.get('display', 'summarized')})"
        return "disabled"

    def set_thinking_config(self, config) -> dict:
        normalized = self._normalize_thinking_config(config)
        if normalized is None:
            raise ValueError("Unsupported thinking config.")
        self.default_thinking_config = normalized
        self._thinking_config_explicit = True
        self.tool_context["thinking_config_explicit"] = True
        self.tool_context["thinking_config"] = self.default_thinking_config
        return copy.deepcopy(self.default_thinking_config)

    def set_mode_thinking_config(self, mode: str, config) -> dict:
        mode_name = str(mode or "").strip()
        if not mode_name:
            raise ValueError("mode is required.")
        normalized = self._normalize_thinking_config(config)
        if normalized is None:
            self.mode_thinking_overrides.pop(mode_name, None)
        else:
            self.mode_thinking_overrides[mode_name] = normalized
        self.tool_context["mode_thinking_overrides"] = self.mode_thinking_overrides
        self._thinking_config_explicit = True
        self.tool_context["thinking_config_explicit"] = True
        return copy.deepcopy(self.mode_thinking_overrides)

    def get_thinking_config(self, mode: str = None) -> dict:
        if mode is None:
            return copy.deepcopy(self._resolve_thinking_config())
        return copy.deepcopy(
            self.mode_thinking_overrides.get(str(mode), self.default_thinking_config)
        )

    def _resolve_thinking_config(self) -> dict:
        mode_name = self.get_mode()
        effective = self.mode_thinking_overrides.get(mode_name, self.default_thinking_config)
        normalized = self._normalize_thinking_config(effective)
        return normalized or {"type": "disabled"}

    def refresh_memory_files(self):
        self.memory_file_manager.set_cwd(self.session_cwd)
        loaded = self.memory_file_manager.refresh()
        self.tool_context["memory_files"] = self.memory_file_manager.export_state()
        return loaded

    def get_memory_summary(self) -> str:
        return self.memory_file_manager.render_prompt_summary()

    def set_structured_output_request(self, request: Optional[dict]):
        result = self.structured_output_manager.set_request(request)
        self.tool_context["structured_output_state"] = self.structured_output_manager.export_state()
        return result

    def clear_structured_output_request(self):
        self.structured_output_manager.clear()
        self.tool_context["structured_output_state"] = self.structured_output_manager.export_state()

    def _build_structured_output_feedback(self, reason: str) -> str:
        return (
            "<system-reminder>\n"
            "The previous final answer failed the structured output requirement.\n"
            "Retry and return only a valid structured response.\n"
            f"Validation failure:\n{reason}\n"
            "</system-reminder>"
        )

    async def _apply_post_sampling_hooks(
        self,
        *,
        final_answer: str,
        turn: int,
        event_callback=None,
    ):
        result = self.hook_manager.evaluate_post_sampling_hooks({
            "cwd": self.session_cwd,
            "mode": self.get_mode(),
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "final_answer": final_answer,
            "session_token_usage": dict(self.session_token_usage),
        })
        if not result.outputs:
            return

        appended_messages = []
        for item in result.outputs:
            severity = item.get("severity", "info")
            rendered = item.get("rendered", "")
            message = {
                "role": "user",
                "content": (
                    "<system-reminder>\n"
                    f"[Hook:PostSamplingHook #{item.get('hook_index')} severity={severity}]\n"
                    f"{rendered}\n"
                    "</system-reminder>"
                ),
            }
            appended_messages.append(message)
            self.post_sampling_history.insert(0, {
                "turn": turn,
                "hook_index": item.get("hook_index"),
                "severity": severity,
                "behavior": item.get("behavior", "observe"),
                "rendered": rendered,
            })

        self.post_sampling_history = self.post_sampling_history[:20]
        self.messages.extend(appended_messages)
        self._sync_message_context()
        await self._record_loop_transition(
            "post_sampling_hook",
            turn=turn,
            event_callback=event_callback,
            count=len(result.outputs),
        )
        await self._emit_stream_event(event_callback, {
            "type": "post_sampling_hook",
            "turn": turn,
            "outputs": result.outputs,
        })

    def _build_cache_compaction_signal(self, recent_turns: int = 5) -> dict:
        recent_records = self.token_usage_history[:recent_turns]
        recent_cache_creation = sum(
            int(item.get("cache_creation_input_tokens", 0) or 0)
            for item in recent_records
        )
        recent_cache_read = sum(
            int(item.get("cache_read_input_tokens", 0) or 0)
            for item in recent_records
        )
        cache_pressure_tokens = max(0, recent_cache_creation - recent_cache_read)
        cache_hit_ratio = (
            recent_cache_read / max(1, recent_cache_creation + recent_cache_read)
        )
        return {
            "recent_turns": len(recent_records),
            "recent_cache_creation_input_tokens": recent_cache_creation,
            "recent_cache_read_input_tokens": recent_cache_read,
            "session_cache_creation_input_tokens": int(
                self.session_token_usage.get("cache_creation_input_tokens", 0) or 0
            ),
            "session_cache_read_input_tokens": int(
                self.session_token_usage.get("cache_read_input_tokens", 0) or 0
            ),
            "cache_pressure_tokens": cache_pressure_tokens,
            "cache_hit_ratio": round(cache_hit_ratio, 3),
        }

    def _build_spill_recorder(self, *, tool_name: str, tool_use_id: str):
        def _record_spill(*, file_path: str, original_content_chars: int = 0, metadata: dict = None):
            self.content_replacement_manager.register_spill(
                file_path=file_path,
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                original_content_chars=original_content_chars,
                metadata=metadata,
            )
            self.tool_context["content_replacement_state"] = self.content_replacement_manager.export_state()
        return _record_spill

    def get_plan(self) -> str:
        return self.plan_manager.get_plan()

    def get_todo_summary(self) -> str:
        items = self.todo_manager.export()
        if not items:
            return "Current structured todo list is empty."

        lines = [f"Structured Todos ({len(items)})"]
        for item in items:
            lines.append(f"- [{item.get('status')}] {item.get('id')}: {item.get('content')}")
        return "\n".join(lines)

    def get_sandbox_summary(self) -> str:
        status = self.sandbox_manager.get_status()
        if not status.get("active"):
            return "No active sandbox."

        lines = [
            f"Sandbox mode: {status.get('mode')}",
            f"Branch: {status.get('branch')}",
            f"Path: {status.get('path')}",
            f"Root: {status.get('root')}",
        ]
        if "dirty" in status:
            lines.append(f"Dirty: {status.get('dirty')}")
        if "untracked_files" in status:
            lines.append(f"Untracked files: {status.get('untracked_files')}")
        if "head_commit" in status:
            lines.append(f"Head commit: {status.get('head_commit')}")
        return "\n".join(lines)

    def get_agents_summary(self) -> str:
        records = self.subagent_registry or []
        if not records:
            return "No subagents tracked in this session."

        lines = [f"Tracked Subagents ({len(records)})"]
        for item in records:
            lines.append(
                f"- {item.get('agent_id')} [{item.get('status', 'unknown')}] depth={item.get('agent_depth', '?')} task={item.get('task', '')}"
            )
        return "\n".join(lines)

    def get_mode_summary(self) -> str:
        return (
            f"Current mode: {self.get_mode()}\n"
            f"Plan present: {'yes' if bool(self.get_plan()) else 'no'}\n"
            f"Todos tracked: {len(self.todo_manager.export())}\n"
            f"Sandbox active: {'yes' if self.sandbox_manager.get_status().get('active') else 'no'}"
        )

    def get_tools_summary(self) -> str:
        lines = []
        if self.in_progress_tools:
            lines.append(f"In-progress tools ({len(self.in_progress_tools)})")
            for item in self.in_progress_tools.values():
                lines.append(
                    f"- {item.get('tool_name')} [{item.get('status')}] {item.get('input_preview')}"
                )
        else:
            lines.append("In-progress tools: none")

        lines.append("")
        if self.recent_tool_activity:
            lines.append(f"Recent tool activity ({len(self.recent_tool_activity)})")
            for item in self.recent_tool_activity[:8]:
                lines.append(
                    f"- {item.get('tool_name')} [{item.get('status')}] {item.get('result_preview')}"
                )
        else:
            lines.append("Recent tool activity: none")

        return "\n".join(lines)

    def get_runtime_summary(self) -> str:
        self.refresh_memory_files()
        shell_tasks = self.shell_task_manager.list_tasks()
        running_shell_tasks = [item for item in shell_tasks if item.get("status") == "running"]
        running_subagents = [item for item in self.subagent_registry if item.get("status") == "running"]
        in_progress_todos = [
            item for item in self.todo_manager.export()
            if item.get("status") == "in_progress"
        ]
        browser_state = self.browser_manager.export_state()
        estimated_tokens = self.compactor.estimate_tokens(self.messages)
        last_transition = self.loop_transition_history[0] if self.loop_transition_history else None
        permission_state = self.permission_manager.export_state()
        denial_history = permission_state.get("denial_history", [])
        pending_permission = permission_state.get("pending_request") or {}
        active_denials = {
            key: value
            for key, value in dict(permission_state.get("consecutive_denials", {}) or {}).items()
            if int(value or 0) > 0
        }
        memory_state = self.memory_file_manager.export_state()
        loaded_memory_files = memory_state.get("loaded_files", [])
        file_state = self.file_state_cache.export_state()
        cached_files = file_state.get("entries", {})
        artifact_state = self.artifact_tracker.export_state()
        commit_history = artifact_state.get("commit_history", []) or []
        incremental_write_state = self.incremental_write_queue.export_state()
        auto_commit_state = self.auto_commit_manager.export_state()
        vcr_state = self.vcr_manager.export_state()
        security_classifier_state = self.security_classifier.export_state()
        file_change_count = sum(
            len(items) for items in dict(artifact_state.get("file_history", {}) or {}).values()
        )
        content_replacement_state = self.content_replacement_manager.export_state()
        replacement_registry = dict(content_replacement_state.get("registry", {}) or {})
        cache_signal = self._build_cache_compaction_signal()

        lines = [
            f"Mode: {self.get_mode()}",
            f"Model provider: {self.model_provider}",
            f"Primary model: {self.primary_model}",
            f"Fallback model: {self.fallback_model}",
            f"Thinking config: {self._thinking_config_to_text(self._resolve_thinking_config())}",
            f"Token estimator: {self.compactor.token_estimator_mode()}",
            f"Local tokenizer path: {self.local_tokenizer_path or 'none'}",
            f"Structured output active: {'yes' if self.structured_output_manager.request else 'no'}",
            f"Memory files loaded: {len(loaded_memory_files)}",
            f"Cached file states: {len(cached_files)}",
            f"Tracked attachments: {len(artifact_state.get('attachments', []) or [])}",
            f"Tracked prefetches: {len(artifact_state.get('prefetch_history', []) or [])}",
            f"Tracked file changes: {file_change_count}",
            f"Tracked commits: {len(commit_history)}",
            f"Incremental write queue events: {len(incremental_write_state.get('history', []) or [])}",
            f"Auto-commit proposals tracked: {len(auto_commit_state.get('proposal_history', []) or [])}",
            f"Content replacements tracked: {len(replacement_registry)}",
            f"Content replacement cleanups: {len(content_replacement_state.get('cleanup_history', []) or [])}",
            f"Post-sampling reports: {len(self.post_sampling_history)}",
            f"Security classifier mode: {security_classifier_state.get('mode', 'off')}",
            f"Security classifier hits: {len(security_classifier_state.get('history', []) or [])}",
            f"VCR mode: {vcr_state.get('mode', 'off')}",
            f"VCR cassettes touched: {len(vcr_state.get('history', []) or [])}",
            f"Estimated context tokens: {estimated_tokens}",
            f"Recent cache creation tokens: {cache_signal.get('recent_cache_creation_input_tokens', 0)}",
            f"Recent cache read tokens: {cache_signal.get('recent_cache_read_input_tokens', 0)}",
            f"Cache-aware compaction pressure: {cache_signal.get('cache_pressure_tokens', 0)}",
            f"Session input tokens: {self.session_token_usage.get('input_tokens', 0)}",
            f"Session output tokens: {self.session_token_usage.get('output_tokens', 0)}",
            f"Remaining output budget: {self._remaining_output_budget(self._build_loop_state())}",
            f"In-progress tools: {len(self.in_progress_tools)}",
            f"Running shell tasks: {len(running_shell_tasks)}",
            f"Running subagents: {len(running_subagents)}",
            f"In-progress todos: {len(in_progress_todos)}",
            f"Permission denials tracked: {len(denial_history)}",
            f"Tools with active denial counts: {len(active_denials)}",
            f"Pending permission request: {'yes' if pending_permission else 'no'}",
            f"Pending auto-commit proposal: {'yes' if auto_commit_state.get('pending_proposal') else 'no'}",
            f"Browser available: {'yes' if browser_state.get('available') else 'no'}",
            f"Browser last URL: {browser_state.get('last_url') or 'none'}",
            f"Session persistence: {'ok' if self.last_persist_ok else 'error'}",
            f"Last session file: {self.session_manager.last_save_path or 'unknown'}",
            (
                f"Persist error: {self.last_persist_error}"
                if self.last_persist_error
                else "Persist error: none"
            ),
        ]
        if last_transition:
            lines.append(
                f"Last loop transition: {last_transition.get('reason')} (turn {last_transition.get('turn')})"
            )
        if denial_history:
            last_denial = denial_history[0]
            lines.append(
                "Last permission denial: "
                f"{last_denial.get('tool_name')} via {last_denial.get('source')} "
                f"(count={last_denial.get('count')})"
            )
        if pending_permission:
            lines.append(
                "Pending permission detail: "
                f"{pending_permission.get('tool_name')} "
                f"(risk={pending_permission.get('risk_level')}, mode={pending_permission.get('mode')})"
            )
        if commit_history:
            last_commit = commit_history[0]
            lines.append(
                "Last tracked commit: "
                f"{last_commit.get('status')} "
                f"{last_commit.get('commit_hash') or '<none>'} "
                f"on {last_commit.get('branch') or '<detached>'} "
                f"via {last_commit.get('agent_role') or 'agent'}"
            )
        pending_auto_commit = auto_commit_state.get("pending_proposal") or {}
        if pending_auto_commit:
            lines.append(
                "Pending auto-commit detail: "
                f"{pending_auto_commit.get('branch') or '<detached>'} "
                f"{pending_auto_commit.get('message_preview') or '<no message>'}"
            )
        return "\n".join(lines)

    def _sync_message_context(self):
        self.tool_context["messages"] = self.messages
        self.tool_context["subagent_registry"] = self.subagent_registry
        self.tool_context["in_progress_tools"] = self.in_progress_tools
        self.tool_context["recent_tool_activity"] = self.recent_tool_activity
        self.tool_context["loop_transition_history"] = self.loop_transition_history
        self.tool_context["post_sampling_history"] = self.post_sampling_history
        self.tool_context["permission_state"] = self.permission_manager.export_state()
        self.tool_context["thinking_config"] = self.default_thinking_config
        self.tool_context["mode_thinking_overrides"] = self.mode_thinking_overrides
        self.tool_context["memory_files"] = self.memory_file_manager.export_state()
        self.tool_context["structured_output_state"] = self.structured_output_manager.export_state()
        self.tool_context["security_classifier_state"] = self.security_classifier.export_state()
        self.tool_context["session_id"] = self.session_id
        self.tool_context["model_provider"] = self.model_provider
        self.tool_context["primary_model"] = self.primary_model
        self.tool_context["fallback_model"] = self.fallback_model
        self.tool_context["api_base_url"] = self.api_base_url
        self.tool_context["local_tokenizer_path"] = self.local_tokenizer_path
        self.tool_context["read_file_state"] = {
            path: True for path, entry in self.file_state_cache.export_state().get("entries", {}).items()
            if entry.get("has_been_read")
        }
        self.tool_context["file_state_cache_state"] = self.file_state_cache.export_state()
        self.tool_context["artifact_tracker_state"] = self.artifact_tracker.export_state()
        self.tool_context["incremental_write_queue_state"] = self.incremental_write_queue.export_state()
        self.tool_context["auto_commit_state"] = self.auto_commit_manager.export_state()
        self.tool_context["content_replacement_state"] = self.content_replacement_manager.export_state()
        self.tool_context["vcr_state"] = self.vcr_manager.export_state()

    def _record_tool_start(self, tool_name: str, tool_use_id: str, tool_inputs: dict):
        self.in_progress_tools[tool_use_id] = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "status": "running",
            "input_preview": str(tool_inputs)[:220],
        }
        self._sync_message_context()
        self._emit_tool_activity("tool_started", tool_name, tool_use_id, input=copy.deepcopy(tool_inputs))

    def _emit_tool_activity(self, kind, tool_name, tool_use_id, **payload):
        callback = self.tool_context.get("capability_event")
        if callback:
            callback({"type": kind, "tool_name": tool_name, "tool_use_id": tool_use_id,
                      "agent_id": self.agent_id if self.agent_depth else None,
                      "agent_role": self.agent_role, "timestamp": int(time.time() * 1000), **payload})

    def inherit_state_from_parent(
        self,
        parent_messages=None,
        todo_payload=None,
        plan_payload=None,
    ):
        if parent_messages:
            self.messages = copy.deepcopy(parent_messages)
            self._sync_message_context()
        if todo_payload is not None:
            self.todo_manager.load(copy.deepcopy(todo_payload))
        if plan_payload is not None:
            self.plan_manager.load(
                mode=plan_payload.get("mode", "normal"),
                content=plan_payload.get("content", ""),
            )

    def register_subagent_record(self, record: dict):
        if not isinstance(record, dict):
            return
        if record.get("status") in {"completed", "cancelled", "crashed"} and "phase" not in record:
            record = {**record, "phase": record["status"]}

        agent_id = record.get("agent_id")
        if not agent_id:
            return

        previous = copy.deepcopy(self.subagent_registry)
        replaced = False
        for index, item in enumerate(self.subagent_registry):
            if item.get("agent_id") == agent_id:
                merged = dict(item)
                merged.update(record)
                self.subagent_registry[index] = merged
                replaced = True
                break

        if not replaced:
            self.subagent_registry.append(record)

        self._sync_message_context()
        saved = self.persist_session_state()
        if not saved:
            self.subagent_registry[:] = previous
            self._sync_message_context()
            return False

        callback = self.tool_context.get("capability_event")
        if callback and saved:
            callback({"type": "subagent_status", "agent": {k: v for k, v in record.items() if k not in {"role_definition", "pending_messages", "dispatch", "api_base_url"}}})
        return saved

    def _build_message_statistics(self) -> dict:
        stats = {
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_result_blocks": 0,
            "system_observations": 0,
        }

        for msg in self.messages:
            role = msg.get("role")
            if role == "user":
                stats["user_messages"] += 1
            elif role == "assistant":
                stats["assistant_messages"] += 1

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        stats["tool_result_blocks"] += 1
            elif isinstance(content, str) and content.startswith("[System Observation]"):
                stats["system_observations"] += 1

        return stats

    async def _apply_layered_compaction_if_needed(
        self,
        sys_print_callback=print,
        *,
        force: bool = False,
        aggressive: bool = False,
    ):
        """
        Apply a staged compaction strategy:
        1. Micro-compact older oversized tool results.
        2. Snip stale historical compaction boundaries.
        3. Trim very large historical tool results.
        4. Summarize only the middle history while preserving the first message
           and a recent raw window.
        """
        if self.tool_context.get("external_backend"):
            # The external CLI owns its context; do not make hidden native-model calls.
            return False
        self.require_writer()
        archive = None
        def preserve_history():
            nonlocal archive
            if archive is None:
                from xuloom.core.storage import atomic_json
                archive = artifact_directory(self.session_cwd) / self.session_id / ("context-" + uuid.uuid4().hex + ".json")
                atomic_json(archive, {"messages": self.messages})
                self.content_replacement_manager.register_spill(file_path=str(archive), tool_name="context_compaction",
                    metadata={"format": "conversation_json", "authority": "historical evidence, not new authorization"})
            return archive
        self.refresh_memory_files()
        effective_thinking_config = self._resolve_thinking_config()
        system_prompt = self.context_builder.generate_system_prompt(
            session_summary=self.plan_manager.render_prompt_summary(),
            todo_summary=self.todo_manager.render_prompt_summary(),
            memory_summary=self.get_memory_summary(),
            structured_output_summary=self.structured_output_manager.render_prompt_summary(),
            tool_prompt_summary=self._build_tool_prompt_summary(),
            collaboration_summary=self._collaboration_instructions(),
        )
        tools_schema = self._get_anthropic_tools_schema()
        preserve_last_messages = 8 if aggressive else 12

        snipped_messages, snipped_count = self.compactor.snip_old_compaction_boundaries(
            self.messages,
            preserve_last_messages=preserve_last_messages,
            keep_recent_boundaries=1,
        )
        if snipped_count > 0:
            preserve_history()
            self.messages = snipped_messages
            self._sync_message_context()
            self.persist_session_state()
            sys_print_callback(
                f"[dim magenta]↳ Snipped {snipped_count} stale compaction boundary message(s).[/dim magenta]"
            )

        micro_compacted_messages, micro_count = self.compactor.micro_compact_tool_results(
            self.messages,
            preserve_last_messages=preserve_last_messages,
            max_tool_result_chars=800 if aggressive else 1200,
            preview_chars=200 if aggressive else 300,
        )
        if micro_count > 0:
            preserve_history()
            self.messages = micro_compacted_messages
            self._sync_message_context()
            self.persist_session_state()
            sys_print_callback(
                f"[dim magenta]↳ Micro-compacted {micro_count} historical tool result(s).[/dim magenta]"
            )

        max_tool_result_chars = 800 if aggressive else 1200
        trimmed_messages, trimmed_count = self.compactor.prune_large_tool_results(
            self.messages,
            preserve_last_messages=preserve_last_messages,
            max_tool_result_chars=max_tool_result_chars,
        )
        if trimmed_count > 0:
            preserve_history()
            self.messages = trimmed_messages
            self._sync_message_context()
            self.persist_session_state()
            sys_print_callback(
                f"[dim magenta]↳ Trimmed {trimmed_count} oversized historical tool result(s) before compaction.[/dim magenta]"
            )

        cache_signal = self._build_cache_compaction_signal()
        api_messages = self._clean_roles_for_api(self.messages)
        context_window = int(os.environ.get("XULOOM_CONTEXT_WINDOW", "0")) or 200000
        budget = int(context_window * 0.75) if aggressive else int(context_window * 0.80)
        reserve = int(context_window * 0.08) if aggressive else int(context_window * 0.06)
        should_compact, estimated_tokens, estimate_source = await self.compactor.should_compact_precise(
            api_messages,
            model=self.primary_model,
            system=system_prompt,
            tools=tools_schema,
            thinking=effective_thinking_config,
            token_budget=budget,
            reserve_tokens=reserve,
            hard_message_count=80 if aggressive else 100,
            cache_read_input_tokens=cache_signal.get("recent_cache_read_input_tokens", 0),
            cache_creation_input_tokens=cache_signal.get("recent_cache_creation_input_tokens", 0),
            max_cache_penalty_tokens=14000 if aggressive else 10000,
        )
        if not force and not should_compact:
            return False

        sys_print_callback(
            "\n[bold magenta]⎔ Context Window Critical: Triggering layered context compaction...[/bold magenta]\n"
            f"[dim magenta]↳ Estimated context tokens: {estimated_tokens} (source={estimate_source})[/dim magenta]\n"
            f"[dim magenta]↳ Cache-aware pressure: {cache_signal.get('cache_pressure_tokens', 0)} "
            f"(creation={cache_signal.get('recent_cache_creation_input_tokens', 0)}, "
            f"read={cache_signal.get('recent_cache_read_input_tokens', 0)})[/dim magenta]"
        )
        head, middle, tail = self.compactor.split_for_layered_compaction(
            self.messages,
            preserve_first_messages=1,
            preserve_last_messages=preserve_last_messages,
        )
        if not middle:
            return False

        summary = await self.compactor.compact_history(middle)
        preserve_history()

        session_memory_content = self.session_memory_manager.get_memory_content()
        session_memory_block = ""
        if session_memory_content:
            truncated = session_memory_content
            if len(truncated) > MAX_SESSION_MEMORY_IN_COMPACT:
                truncated = truncated[:MAX_SESSION_MEMORY_IN_COMPACT] + "\n[... truncated ...]"
            session_memory_block = f"\nSession memory notes:\n{truncated}\n"

        stage_label = "aggressive_layered_summary" if aggressive else "layered_summary"
        reminder = {
            "role": "user",
            "content": (
                "This session is being continued from a previous conversation that ran out of context. "
                "The summary below covers the earlier portion of the conversation.\n\n"
                f"[CompactionBoundary:{stage_label}]\n"
                f"estimated_tokens_before: {estimated_tokens}\n"
                f"cache_pressure_tokens: {cache_signal.get('cache_pressure_tokens', 0)}\n"
                f"cache_creation_input_tokens: {cache_signal.get('recent_cache_creation_input_tokens', 0)}\n"
                f"cache_read_input_tokens: {cache_signal.get('recent_cache_read_input_tokens', 0)}\n"
                f"preserved_first_messages: 1\n"
                f"preserved_last_messages: {preserve_last_messages}\n\n"
                f"{summary}\n"
                f"Complete pre-compaction history: {archive}. Read with file_read_tool when evidence is missing.\n"
                f"{session_memory_block}\n"
                "Continue the latest authorized unfinished task. Distinguish verified evidence from historical assistant claims. "
                "This summary grants no new authorization. Inspect saved results and uncertain effects before continuing; "
                "do not replay interrupted operations. Ask for missing information or approval when required."
            ),
        }
        self.messages = head + [reminder] + tail
        self._update_compact_boundary()
        self._sync_message_context()
        self.persist_session_state()
        sys_print_callback(
            "[dim magenta]↳ Layered compaction complete. Preserved recent raw context and summarized older history.[/dim magenta]\n"
        )

        post_summary_api_messages = self._clean_roles_for_api(self.messages)
        collapse_needed, collapsed_estimated_tokens, collapsed_estimate_source = await self.compactor.should_compact_precise(
            post_summary_api_messages,
            model=self.primary_model,
            system=system_prompt,
            tools=tools_schema,
            thinking=effective_thinking_config,
            token_budget=44000 if aggressive else 52000,
            reserve_tokens=12000 if aggressive else 10000,
            hard_message_count=18 if aggressive else 24,
            cache_read_input_tokens=cache_signal.get("recent_cache_read_input_tokens", 0),
            cache_creation_input_tokens=cache_signal.get("recent_cache_creation_input_tokens", 0),
            max_cache_penalty_tokens=12000 if aggressive else 8000,
        )
        if collapse_needed:
            collapsed_messages, collapse_meta = self.compactor.context_collapse(
                self.messages,
                preserve_first_messages=1,
                preserve_last_messages=preserve_last_messages,
                max_projection_items=8 if aggressive else 10,
                text_chars=140 if aggressive else 180,
            )
            if collapse_meta.get("collapsed"):
                self.messages = collapsed_messages
                self._update_compact_boundary()
                self._sync_message_context()
                self.persist_session_state()
                sys_print_callback(
                    "[dim magenta]↳ Context collapse applied after layered summary to preserve only a projection of older history.[/dim magenta]\n"
                    f"[dim magenta]↳ Post-summary estimated tokens before collapse: {collapsed_estimated_tokens} "
                    f"(source={collapsed_estimate_source})[/dim magenta]"
                )

        restored = self._post_compact_restore_state(sys_print_callback)
        if restored:
            self._sync_message_context()
            self.persist_session_state()
        return True

    POST_COMPACT_MAX_FILES = 5
    POST_COMPACT_TOKEN_BUDGET = 50_000

    def _post_compact_restore_state(self, sys_print_callback=print) -> bool:
        """
        Re-inject critical context after compaction so the agent doesn't lose
        awareness of recently-read files, the active plan, or loaded skills.
        Mirrors Claude Code's post-compact state recovery in compact.ts.
        """
        restore_parts = []
        budget_remaining = self.POST_COMPACT_TOKEN_BUDGET

        sources = sorted((entry for entry in self.file_state_cache.entries.values() if entry.get("has_been_read")),
            key=lambda entry: entry.get("mtime", 0), reverse=True)[:self.POST_COMPACT_MAX_FILES]
        files_restored = len(sources)
        if sources:
            restore_parts.append("[Previously read sources: metadata only; contents may have changed. Use authorized file tools when needed.]\n" +
                json.dumps([{key: entry.get(key) for key in ("path", "sha256", "mtime")} for entry in sources], ensure_ascii=False))
        if self.subagent_registry or self.in_progress_tools:
            restore_parts.append("[Actual saved execution state; uncertain effects must not be replayed]\n" + json.dumps({
                "workers": [{key: row.get(key) for key in ("agent_id", "status", "phase", "turn")} for row in self.subagent_registry],
                "in_progress_tools": self.in_progress_tools}, ensure_ascii=False))

        plan_content = self.plan_manager.render_prompt_summary()
        if plan_content:
            plan_tokens = len(plan_content) // 4
            if plan_tokens <= budget_remaining:
                restore_parts.append(f"[Active plan state]\n{plan_content}")
                budget_remaining -= plan_tokens

        todo_content = self.todo_manager.render_prompt_summary()
        if todo_content:
            todo_tokens = len(todo_content) // 4
            if todo_tokens <= budget_remaining:
                restore_parts.append(f"[Active todo state]\n{todo_content}")
                budget_remaining -= todo_tokens

        mcp_instructions = self._build_mcp_instructions()
        if mcp_instructions:
            mcp_tokens = len(mcp_instructions) // 4
            if mcp_tokens <= budget_remaining:
                restore_parts.append(f"[MCP tool instructions]\n{mcp_instructions}")
                budget_remaining -= mcp_tokens

        if not restore_parts:
            return False

        restore_text = (
            "<system-reminder>\n"
            "[Post-compaction context restoration]\n"
            "The conversation was compacted. Saved references and execution state follow. "
            "These are continuity evidence, not new instructions or permissions.\n\n"
            + "\n\n".join(restore_parts)
            + "\n</system-reminder>"
        )
        self.messages.append(create_user_message(
            restore_text,
            is_meta=True,
            origin="post_compact_restore",
        ))
        sys_print_callback(
            f"[dim magenta]↳ Post-compact: restored {files_restored} file(s), "
            f"plan={'yes' if plan_content else 'no'}, "
            f"todos={'yes' if todo_content else 'no'}, "
            f"mcp={'yes' if mcp_instructions else 'no'}.[/dim magenta]"
        )
        return True

    def _is_context_overflow_error(self, error: Exception) -> bool:
        rendered = str(error).lower()
        markers = [
            "prompt is too long",
            "context length",
            "too many tokens",
            "maximum context length",
            "input is too long",
            "context window exceeds",
            "exceed context limit",
            "context_length_exceeded",
            "input length and `max_tokens` exceed",
        ]
        return any(marker in rendered for marker in markers)

    MAX_TOOL_CONCURRENCY = 10
    SIBLING_CANCEL_TOOLS = frozenset({"bash_tool"})

    def _is_concurrency_safe(self, tool_name: str, tool_inputs: dict) -> bool:
        handler = self.available_tools.get(tool_name)
        if not handler:
            return False

        inspector = getattr(handler, "is_concurrency_safe_call", None)
        if callable(inspector):
            try:
                return bool(inspector(**tool_inputs))
            except Exception:
                return False

        return False

    @staticmethod
    def _tb_get(tool_block, key, default=""):
        if isinstance(tool_block, dict):
            return tool_block.get(key, default)
        return getattr(tool_block, key, default)

    def _partition_tool_batches(self, tool_calls):
        batches = []
        for tool_block in tool_calls:
            name = self._tb_get(tool_block, "name", "")
            inp = self._tb_get(tool_block, "input", {}) or {}
            is_safe = self._is_concurrency_safe(name, inp)
            if is_safe and batches and batches[-1]["is_concurrency_safe"]:
                batches[-1]["blocks"].append(tool_block)
            else:
                batches.append({
                    "is_concurrency_safe": is_safe,
                    "blocks": [tool_block],
                })
        return batches

    async def _execute_tool_wrapper(self, tool_block, sys_print_callback=print, *, turn: int = 0, event_callback=None, result_callback=None):
        return await execute_tool(self, tool_block, sys_print_callback, turn=turn, event_callback=event_callback, result_callback=result_callback)

    async def agents(self, action="list", **parameters):
        """Versioned host API over the existing authorized agent tool and execution journal."""
        identity = "host-" + uuid.uuid4().hex
        captured = []
        inputs = {**parameters, "action": action, "response_format": "json"}
        # Keep the versioned host API's blocking default; model calls launch in
        # the background unless explicitly requested otherwise.
        inputs.setdefault("run_in_background", False)
        try:
            self.require_writer()
            receipt = await self._execute_tool_wrapper({"id": identity, "name": "agent_tool", "input": inputs},
                lambda _: None, result_callback=captured.append)
            if receipt["is_error"]:
                return {"api_version": 1, "ok": False, "session_id": self.session_id, "tool_use_id": identity,
                    "error": {"code": "tool_error", "message": receipt["content"]}, "data": None}
            data = json.loads(captured[0])
            return {"api_version": 1, "ok": True, "session_id": self.session_id, "tool_use_id": identity, "data": data}
        except (PersistenceError, SessionBusyError, SessionConflictError) as error:
            return {"api_version": 1, "ok": False, "session_id": self.session_id, "tool_use_id": identity,
                "error": {"code": "persistence_error" if isinstance(error, PersistenceError) else "session_busy" if isinstance(error, SessionBusyError) else "session_conflict", "message": str(error)}, "data": None}

    async def _execute_tool_batches(self, tool_calls, sys_print_callback=print, *, turn: int = 0, event_callback=None):
        return await execute_tool_batches(self, tool_calls, sys_print_callback, turn=turn, event_callback=event_callback)
            
    async def start_protocols(self, sys_print=print):
        self._assert_open()
        if self.execution_sandbox.enabled:
            sys_print("System workspace isolation: external MCP and LSP processes are disabled")
            return
        # 1. Start LSP Daemon Support
        self.lsp_manager = LSPManager()
        self.tool_context["lsp_manager"] = self.lsp_manager
        lsp_t = LspTool()
        lsp_t.context = self.tool_context
        self.latent_tools["lsp_tool"] = lsp_t
        sys_print("[dim green]↳ LSP Manager Established (Passive Telemetry & AST active)[/dim green]")
        
        # 2. Start MCP Background Connections
        self.mcp_bridge = MCPBridge(str(configuration_file(self.session_cwd, "mcp_servers.json")))
        remote_tools = await self.mcp_bridge.load_and_connect()
        for name, status in self.mcp_bridge.statuses.items():
            if status.get("status") == "error":
                sys_print("MCP " + name + ": " + status["error"])
        if remote_tools:
            self.register_mcp_tools(remote_tools)
            sys_print(f"[dim green]↳ Protocol Matrix Online: Discovered {len(remote_tools)} MCP tools; load details with tool_search_tool.[/dim green]")
        
    def _get_anthropic_tools_schema(self):
        """Converts registered Pydantic tool classes into provider-native schemas."""
        effective_tools = self._get_effective_tools()
        schemas = []
        for tool in effective_tools.values():
            definition = tool.get_tool_definition()
            if self.model_provider == "openai":
                schemas.append(self._openai_tool_definition(definition))
            else:
                schemas.append(definition)
        return schemas

    def _block_to_dict(self, block):
        if isinstance(block, dict):
            return copy.deepcopy(block)
        if isinstance(block, str):
            return {"type": "text", "text": block}

        block_type = getattr(block, "type", None)
        if block_type == "text":
            return {
                "type": "text",
                "text": getattr(block, "text", ""),
            }
        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": copy.deepcopy(getattr(block, "input", {}) or {}),
                **({"_argument_error": block._argument_error} if getattr(block, "_argument_error", "") else {}),
                **({"_raw_arguments": getattr(block, "_raw_arguments", "")} if getattr(block, "_argument_error", "") else {}),
            }
        if block_type == "thinking":
            return {
                "type": "thinking",
                "thinking": getattr(block, "thinking", ""),
                "signature": getattr(block, "signature", ""),
            }
        if block_type == "redacted_thinking":
            return {
                "type": "redacted_thinking",
                "data": getattr(block, "data", ""),
            }

        text_value = getattr(block, "text", None)
        if isinstance(text_value, str):
            return {"type": "text", "text": text_value}
        return None

    def _merge_adjacent_text_blocks(self, blocks: list) -> list:
        merged = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if (
                merged
                and merged[-1].get("type") == "text"
                and block.get("type") == "text"
            ):
                merged[-1]["text"] = (
                    f"{merged[-1].get('text', '')}{block.get('text', '')}"
                )
                continue
            merged.append(block)
        return merged

    def _normalize_message_content_for_api(self, role: str, content):
        if isinstance(content, str):
            normalized = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            normalized = []
            for raw_block in content:
                block = self._block_to_dict(raw_block)
                if not block:
                    continue

                block_type = block.get("type")
                if block_type in {"thinking", "redacted_thinking"}:
                    # Do not feed internal reasoning or signatures back into later turns.
                    continue

                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        normalized.append({"type": "text", "text": text})
                    continue

                if block_type == "tool_use" and role == "assistant":
                    tool_name = block.get("name")
                    tool_use_id = block.get("id")
                    if tool_name and tool_use_id:
                        normalized.append({
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": tool_name,
                            "input": copy.deepcopy(block.get("input", {}) or {}),
                        })
                    continue

                if block_type == "tool_result" and role == "user":
                    tool_use_id = block.get("tool_use_id")
                    if not tool_use_id:
                        continue

                    block_content = block.get("content", "")
                    if isinstance(block_content, list):
                        parts = []
                        for part in block_content:
                            part_block = self._block_to_dict(part)
                            if isinstance(part_block, dict) and part_block.get("type") in {"text", "image", "document"}:
                                parts.append(part_block)
                        block_content = parts if any(part.get("type") in {"image", "document"} for part in parts) else "".join(part.get("text", "") for part in parts)
                    elif block_content is None:
                        block_content = ""
                    else:
                        block_content = str(block_content)

                    normalized.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": block_content,
                        "is_error": bool(block.get("is_error", False)),
                    })
                    continue

                if block_type in {"image", "document"} and role == "user":
                    normalized.append(block)
                    continue

                fallback_text = block.get("text") or block.get("content")
                if fallback_text:
                    normalized.append({"type": "text", "text": str(fallback_text)})
        else:
            normalized = [{"type": "text", "text": str(content)}]

        normalized = self._merge_adjacent_text_blocks(normalized)
        return [block for block in normalized if isinstance(block, dict)]

    def _filter_incomplete_tool_messages_for_api(self, messages):
        """
        Ensure tool_use / tool_result pairs are always complete.
        Drop assistant messages whose tool_use has no matching tool_result,
        AND strip tool_result blocks whose tool_use_id has no matching tool_use.
        """
        if not messages:
            return []

        tool_use_ids_all = set()
        tool_result_ids_all = set()
        for msg in messages:
            for block in (msg.get("content", []) if isinstance(msg.get("content"), list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("id"):
                    tool_use_ids_all.add(block["id"])
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    tool_result_ids_all.add(block["tool_use_id"])

        filtered = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", [])

            if role == "assistant" and isinstance(content, list):
                use_ids = [
                    b.get("id") for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
                ]
                if use_ids and any(uid not in tool_result_ids_all for uid in use_ids):
                    continue

            if role == "user" and isinstance(content, list):
                cleaned_content = []
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id")
                            and block["tool_use_id"] not in tool_use_ids_all):
                        continue
                    cleaned_content.append(block)
                if not cleaned_content:
                    continue
                msg = {**msg, "content": cleaned_content}

            filtered.append(msg)

        return filtered

    def _clean_roles_for_api(self, messages):
        """Normalize message content and collapse adjacent roles for Anthropic API calls."""
        normalized_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue

            normalized_content = self._normalize_message_content_for_api(
                role,
                msg.get("content", ""),
            )
            if not normalized_content:
                continue

            normalized_messages.append({
                "role": role,
                "content": normalized_content,
            })

        normalized_messages = self._filter_incomplete_tool_messages_for_api(normalized_messages)
        normalized_messages = self._strip_error_causing_attachments(normalized_messages)

        collapsed = []
        for msg in normalized_messages:
            if not collapsed:
                collapsed.append(msg)
                continue
            if collapsed[-1]["role"] == msg["role"]:
                collapsed[-1]["content"] = self._merge_adjacent_text_blocks(
                    collapsed[-1]["content"] + msg["content"]
                )
            else:
                collapsed.append(msg)

        collapsed = self._filter_orphaned_thinking_only_messages(collapsed)
        collapsed = self._strip_trailing_thinking_from_last_assistant(collapsed)
        collapsed = self._filter_whitespace_only_assistant_messages(collapsed)
        collapsed = self._ensure_non_empty_assistant_content(collapsed)
        return collapsed

    # ------------------------------------------------------------------
    # Post-normalization sanitizers (prevent API 400 in long conversations)
    # ------------------------------------------------------------------

    _ERROR_ATTACHMENT_MARKERS = {
        "pdf is too large": {"document"},
        "document is too large": {"document"},
        "image exceeds": {"image"},
        "image is too large": {"image"},
        "request too large": {"document", "image"},
        "request_too_large": {"document", "image"},
        "payload too large": {"document", "image"},
    }

    def _strip_error_causing_attachments(self, messages: list) -> list:
        """Remove document/image blocks from user messages that triggered 413.

        When a large PDF or image causes a prompt-too-large error, the API
        returns a synthetic error assistant message.  If the offending
        attachment stays in history, every subsequent turn will also 413.
        This method back-tracks from each error message and strips the
        block types that caused it from the nearest prior user message.
        """
        strip_targets: dict[int, set[str]] = {}
        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text_lower = block.get("text", "").lower()
                for marker, block_types in self._ERROR_ATTACHMENT_MARKERS.items():
                    if marker in text_lower:
                        for j in range(i - 1, -1, -1):
                            if messages[j].get("role") == "user":
                                strip_targets.setdefault(j, set()).update(block_types)
                                break

        if not strip_targets:
            return messages

        result = []
        for i, msg in enumerate(messages):
            if i not in strip_targets:
                result.append(msg)
                continue
            types_to_strip = strip_targets[i]
            content = msg.get("content", [])
            if not isinstance(content, list):
                result.append(msg)
                continue
            cleaned = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") in types_to_strip)
            ]
            if cleaned:
                result.append({**msg, "content": cleaned})
        return result

    def _filter_orphaned_thinking_only_messages(self, messages: list) -> list:
        """Remove assistant messages that contain no substantive content.

        After compaction or streaming fallback, the history may contain
        assistant messages that originally had thinking + text/tool_use but
        lost their non-thinking blocks.  Consecutive assistant messages with
        mismatched thinking signatures cause API 400 errors.
        """
        filtered = []
        for msg in messages:
            if msg.get("role") != "assistant":
                filtered.append(msg)
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                filtered.append(msg)
                continue
            has_substantive = any(
                isinstance(b, dict) and b.get("type") in {"text", "tool_use"}
                for b in content
            )
            if has_substantive:
                filtered.append(msg)
        return filtered

    def _strip_trailing_thinking_from_last_assistant(self, messages: list) -> list:
        """Strip thinking/redacted_thinking blocks from the last assistant message.

        The API rejects trailing thinking blocks that lack a valid signature
        continuation.  Order matters: this runs BEFORE the whitespace-only
        filter so that a message like [text("\\n"), thinking("...")] doesn't
        survive the whitespace check only to be rejected later.
        """
        if not messages:
            return messages
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                content = messages[i].get("content", [])
                if isinstance(content, list):
                    cleaned = [
                        b for b in content
                        if not (isinstance(b, dict)
                                and b.get("type") in {"thinking", "redacted_thinking"})
                    ]
                    if cleaned != content:
                        messages[i] = {**messages[i], "content": cleaned}
                break
        return messages

    def _filter_whitespace_only_assistant_messages(self, messages: list) -> list:
        """Drop assistant messages whose text content is only whitespace."""
        filtered = []
        for msg in messages:
            if msg.get("role") != "assistant":
                filtered.append(msg)
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                filtered.append(msg)
                continue
            has_non_whitespace = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "tool_use":
                    has_non_whitespace = True
                    break
                if btype == "text" and b.get("text", "").strip():
                    has_non_whitespace = True
                    break
            if has_non_whitespace:
                filtered.append(msg)
        return filtered

    def _ensure_non_empty_assistant_content(self, messages: list) -> list:
        """Guarantee every assistant message has at least one content block."""
        filtered = []
        for msg in messages:
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list) and not content:
                    continue
            filtered.append(msg)
        return filtered

    def _strip_thinking_signatures_from_history(self):
        """Remove thinking/redacted_thinking blocks from all assistant messages.

        Thinking signatures are model-bound: replaying a protected-thinking
        block produced by one model (e.g. sonnet) to a different fallback
        model (e.g. opus) causes API 400 errors.  This must be called before
        retrying with a different model.
        """
        changed = False
        for msg in self.messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            cleaned = [
                b for b in content
                if not (isinstance(b, dict)
                        and b.get("type") in {"thinking", "redacted_thinking"})
            ]
            if len(cleaned) != len(content):
                msg["content"] = cleaned
                changed = True
        if changed:
            self._sync_message_context()

    def _new_command_sessions(self, sid):
        def notify(row):
            callback = self.tool_context.get("capability_event")
            if callback:
                callback({"type": "command_status", "command": row})
        manager = CommandSessions(artifact_directory(self.session_cwd) / sid / "commands", notify)
        manager.execution_sandbox = self.execution_sandbox
        return manager

    def load_session(self, sid: str) -> bool:
        if self.has_active_turn() or any(not task.done() for task in self.tool_context["subagent_tasks"].tasks.values()):
            raise SessionBusyError("Stop or finish owned work before loading another session")
        for manager in (self.session_memory_manager, self.memory_extractor):
            manager.cancel_pending()
        if self.agent_depth == 0 and sid != self.session_id:
            self.session_manager.release_writer(self.session_id)
        state = self.session_manager.load_session_state(sid)
        msgs = state.get("messages", [])
        delegated = state.get("metadata", {}).get("delegation", {})
        if not isinstance(delegated, dict):
            raise ValueError("Invalid saved delegation metadata")
        if "authority_required" in delegated and delegated["authority_required"] is not True:
            raise ValueError("Invalid saved delegated execution authority")
        if msgs or state.get("_valid"):
            self.session_id = sid
            self.tool_context["command_sessions"] = self._new_command_sessions(sid)
            self.tool_context["command_sessions"].restore()
            self.last_persist_ok = self.session_manager.last_save_ok
            self.last_persist_error = self.session_manager.last_save_error
            self.messages = msgs
            self._sync_message_context()
            metadata = state.get("metadata", {})
            from xuloom.agents.external.claude import restore as restore_external
            restore_external(self, metadata)
            if self.agent_depth == 0:
                previous_delegation = self.tool_context.pop("delegation", {})
                if previous_delegation.get("authority_required"):
                    self.permission_manager.delegated_authorizer = None
            if "authority_required" in delegated:
                self.tool_context["delegation"] = copy.deepcopy(delegated)
            self.tool_context["workflow_completion"].restore(metadata.get("workflow_completion"))
            execution_mode = metadata.get("execution_mode", "host")
            if execution_mode not in {"host", "workspace"}:
                raise ValueError("Invalid saved execution isolation mode")
            self.execution_sandbox.mode = execution_mode
            self.set_collaboration_mode(metadata.get("collaboration_mode", "auto"), persist=False)
            if metadata.get("collaboration_run"):
                run = copy.deepcopy(metadata["collaboration_run"])
                run["output_used"] += run.get("output_reserved", 0)
                run["output_reserved"] = 0
                run["budget"] = {"output_tokens": self.tool_context["collaboration"]["output_tokens"]}
                run["request_cap"] = self.tool_context["collaboration"]["request_output_tokens"]
                self.tool_context["collaboration_run"] = run
            from xuloom.features.goals import recover
            self.goal = recover(metadata.get("goal"))
            self.scheduled_deliveries = copy.deepcopy(metadata.get("scheduled_deliveries", {}))
            restore_tool_activity(self, metadata)
            self.agent_id = metadata.get("agent", {}).get("agent_id") or self.agent_id
            self.tool_context["agent_id"] = self.agent_id
            self.tool_context["file_changes"] = metadata.get("file_changes", [])
            self.tool_context["workspace_moves"] = metadata.get("workspace_moves", [])
            self.turn_traces = metadata.get("turn_traces", [])
            model_runtime = metadata.get("model_runtime", {}) or {}
            # Checkpoints describe past execution; they cannot redirect this
            # instance's current credentials to another endpoint/provider.
            if model_runtime:
                previous_model = self.primary_model
                if model_runtime.get("provider") == self.model_provider and str(model_runtime.get("api_base_url") or "") == self.api_base_url:
                    self.primary_model = str(model_runtime.get("primary_model", self.primary_model) or self.primary_model)
                    self.fallback_model = str(model_runtime.get("fallback_model", self.fallback_model) or self.fallback_model)
                self.model = self.primary_model
                if self.primary_model != previous_model:
                    self.reasoning_profile = configured_profile(self.model_provider, self.primary_model, self.api_base_url)
                self._retired_model_clients.extend(client for client in (self.client, self.compactor.client) if client and not client.is_closed)
                self.client = self._build_model_client()
                self.compactor = MemoryCompactor(
                    model=self.fallback_model or self.primary_model,
                    provider=self.model_provider,
                    api_base_url=self.api_base_url,
                    local_tokenizer_path=self.local_tokenizer_path,
                    api_key=self._api_key,
                    network_mode=self.network_mode,
                )
                self._sync_model_runtime_context()
            self.todo_manager.load(metadata.get("todos", []))
            plan_state = metadata.get("plan", {})
            self.plan_manager.set_session(sid)
            self.plan_manager.load(
                mode=plan_state.get("mode", "normal"),
                content=plan_state.get("content", ""),
            )
            self.sandbox_manager.load_state(metadata.get("sandbox", {}))
            self.browser_manager.load_state(metadata.get("browser", {}))
            self.shell_task_manager.load_state(metadata.get("shell_tasks", []))
            self.permission_manager.load_state(metadata.get("permissions", {}))
            self.memory_file_manager.load_state(metadata.get("memory_files", {}))
            self.file_state_cache.load_state(metadata.get("file_state_cache", {}))
            self.artifact_tracker.load_state(metadata.get("artifacts", {}))
            self.incremental_write_queue.load_state(metadata.get("incremental_write_queue", {}))
            self.auto_commit_manager.load_state(metadata.get("auto_commit", {}))
            self.content_replacement_manager.set_session(self.session_id)
            self.content_replacement_manager.load_state(metadata.get("content_replacements", {}))
            self.structured_output_manager.load_state(metadata.get("structured_output", {}))
            self.security_classifier.load_state(metadata.get("security_classifier", {}))
            self.vcr_manager.load_state(metadata.get("vcr", {}))
            thinking_state = metadata.get("thinking", {})
            self._thinking_config_explicit = thinking_state.get("explicit", bool(thinking_state))
            self.tool_context["thinking_config_explicit"] = self._thinking_config_explicit
            saved_profile = thinking_state.get("profile")
            saved_runtime = metadata.get("model_runtime", {})
            if saved_profile and self.reasoning_profile and saved_runtime.get("primary_model") == self.primary_model and saved_runtime.get("provider") == self.model_provider and saved_runtime.get("api_base_url", "") == self.api_base_url:
                # Keep saved choices only under the current connection's declared capabilities.
                candidate = {**self.reasoning_profile, **{key: saved_profile[key] for key in ("thinking", "effort") if key in saved_profile}}
                try:
                    self.reasoning_profile = normalize_profile(candidate, self.model_provider)
                except ValueError:
                    pass
                self.tool_context["reasoning_profile"] = copy.deepcopy(self.reasoning_profile)
            self.reasoning_effort = thinking_state.get("reasoning_effort")
            self.tool_context["reasoning_effort"] = self.reasoning_effort
            self.default_thinking_config = self._normalize_thinking_config(
                thinking_state.get("default", self.default_thinking_config)
            ) or {"type": "disabled"}
            loaded_mode_overrides = {}
            for mode_name, config in dict(thinking_state.get("mode_overrides", {}) or {}).items():
                normalized = self._normalize_thinking_config(config)
                if normalized is not None:
                    loaded_mode_overrides[str(mode_name)] = normalized
            if loaded_mode_overrides:
                self.mode_thinking_overrides = loaded_mode_overrides
            self.subagent_registry = metadata.get("subagents", [])
            for worker in self.subagent_registry:
                if worker.get("status") == "running":
                    worker["status"] = "interrupted"
                elif worker.get("status") == "queued":
                    worker["phase"] = "awaiting_resume"
            self.team_manager.load_state(metadata.get("team", {}))
            self.ultraplan_manager.load_state(metadata.get("ultraplan", {}))
            self.coordinator_mode = metadata.get("coordinator_mode", False)
            self.loop_transition_history = metadata.get("loop_transitions", [])
            self.token_usage_history = metadata.get("token_usage_history", [])
            self.post_sampling_history = metadata.get("post_sampling_history", [])
            self.session_token_usage = metadata.get("session_token_usage", {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            })
            self.tool_context["token_usage_history"] = self.token_usage_history
            self.tool_context["session_token_usage"] = self.session_token_usage
            self.session_memory_manager.bind_session(self.session_id, metadata.get("session_memory", {}))
            self.memory_extractor.load_state(metadata.get("memory_extractor", {}))
            self._update_compact_boundary()
            self._sync_message_context()
            self.session_started = metadata.get("hooks", {}).get("session_started", False)
            return True
        return False

    def _build_session_metadata(self) -> dict:
        return {
            "external_agent": copy.deepcopy(self.tool_context.get("external_agent")),
            "execution_mode": self.execution_sandbox.mode,
            "collaboration_mode": self.collaboration_mode,
            "collaboration_run": copy.deepcopy(self.tool_context.get("collaboration_run")),
            "goal": self.goal,
            "scheduled_deliveries": self.scheduled_deliveries,
            "file_changes": self.tool_context.get("file_changes", []),
            "tool_activity": {"in_progress": self.in_progress_tools, "recent": self.recent_tool_activity},
            "workspace_moves": self.tool_context.get("workspace_moves", []),
            "todos": self.todo_manager.export(),
            "plan": self.plan_manager.export(),
            "sandbox": self.sandbox_manager.export_state(),
            "browser": self.browser_manager.export_state(),
            "shell_tasks": self.shell_task_manager.export_state(owner=self.agent_id if self.agent_depth else None),
            "permissions": self.permission_manager.export_state(),
            "memory_files": self.memory_file_manager.export_state(),
            "file_state_cache": self.file_state_cache.export_state(),
            "artifacts": self.artifact_tracker.export_state(),
            "incremental_write_queue": self.incremental_write_queue.export_state(),
            "auto_commit": self.auto_commit_manager.export_state(),
            "content_replacements": self.content_replacement_manager.export_state(),
            "structured_output": self.structured_output_manager.export_state(),
            "security_classifier": self.security_classifier.export_state(),
            "vcr": self.vcr_manager.export_state(),
            "thinking": {
                "default": self.default_thinking_config,
                "mode_overrides": self.mode_thinking_overrides,
                "reasoning_effort": self.reasoning_effort,
                "profile": copy.deepcopy(self.reasoning_profile),
                "explicit": self._thinking_config_explicit,
            },
            "subagents": self.subagent_registry,
            "delegation": self.tool_context.get("delegation", {}),
            "turn_traces": self.turn_traces,
            "team": self.team_manager.export_state(),
            "ultraplan": self.ultraplan_manager.export_state(),
            "coordinator_mode": self.coordinator_mode,
            "loop_transitions": self.loop_transition_history,
            "token_usage_history": self.token_usage_history,
            "post_sampling_history": self.post_sampling_history,
            "session_token_usage": self.session_token_usage,
            "message_statistics": self._build_message_statistics(),
            "session_memory": self.session_memory_manager.export_state(),
            "memory_extractor": self.memory_extractor.export_state(),
            "hooks": {
                "session_started": getattr(self, "session_started", False),
            },
            "agent": {
                "agent_id": self.agent_id,
                "parent_agent_id": self.parent_agent_id,
                "agent_depth": self.agent_depth,
                "agent_role": self.agent_role,
            },
            "last_run_result": getattr(self, "last_run_result", None),
            "workflow_completion": self.tool_context["workflow_completion"].export_state(),
            "model_runtime": {
                "provider": self.model_provider,
                "primary_model": self.primary_model,
                "fallback_model": self.fallback_model,
                "api_base_url": self.api_base_url,
                "local_tokenizer_path": self.local_tokenizer_path,
            },
        }

    def persist_session_state(self):
        closing = getattr(self, "_close_task", None)
        if closing is not None and closing.done():
            self.last_persist_ok = False
            self.last_persist_error = "Engine is closed; create a new engine before saving."
            return False
        if self.agent_depth > 0:
            ok = self.session_manager.save_subagent_transcript(
                self.agent_id or self.session_id,
                {"messages": self.messages, "metadata": self._build_session_metadata(),
                 "session_id": self.tool_context.get("delegation", {}).get("owner_session_id")},
            )
            self.last_persist_ok = ok
            return ok

        ok = self.session_manager.save_session_state(
            self.session_id,
            self.messages,
            metadata=self._build_session_metadata(),
        )
        self.last_persist_ok = ok
        self.last_persist_error = self.session_manager.last_save_error

        if not ok and self.last_persist_error != self._last_reported_persist_error:
            print(f"Warning: failed to persist session state: {self.last_persist_error}")
            self._last_reported_persist_error = self.last_persist_error

        if ok:
            self._last_reported_persist_error = ""

        return ok

    def require_writer(self):
        self._assert_open()
        owner = self.tool_context.get("delegation", {}).get("owner_session_id") or self.session_id
        owned = self.session_manager.owns_writer(owner)
        self.session_manager.acquire_writer(owner)
        try:
            self.session_manager.check_current(owner)
        except BaseException:
            if not owned:
                self.session_manager.release_writer(owner)
            raise

    def _assert_open(self):
        if getattr(self, "_close_task", None) is not None:
            raise SessionConflictError("Engine is closing or closed; create a new engine before executing.")

    async def aclose(self):
        task = getattr(self, "_close_task", None)
        if task is None or task.done() and (task.cancelled() or task.exception() is not None):
            task = self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(task)

    async def _close(self):
        goal_task = getattr(self, "_goal_task", None)
        if goal_task is not None and not goal_task.done() and goal_task is not asyncio.current_task():
            if self.goal and self.goal["status"] == "active":
                self.goal["status"], self.goal["reason"] = "interrupted", "Owner closing; explicitly resume after inspecting saved results"
            goal_task.cancel()
            await asyncio.gather(goal_task, return_exceptions=True)
        failures = await asyncio.gather(self.request_abort(), return_exceptions=True)
        failures.extend(await asyncio.gather(self.session_memory_manager.aclose(), self.memory_extractor.aclose(), return_exceptions=True))
        stop = [self.tool_context["command_sessions"].close(owner=self.agent_id if self.agent_depth else None)]
        failures.extend(await asyncio.gather(*stop, return_exceptions=True))
        if self.agent_depth == 0 and self.session_manager.owns_writer(self.session_id) and not any(isinstance(item, BaseException) for item in failures):
            try:
                if not self.persist_session_state():
                    raise PersistenceError("Final checkpoint could not be saved; close failed and the writer lease was retained.")
            except Exception as error:
                failures.append(error)
        cleanup = [self.client.close()]
        if getattr(self.compactor, "client", None):
            cleanup.append(self.compactor.client.close())
        cleanup.extend(client.close() for client in self._retired_model_clients if not client.is_closed)
        if self.agent_depth == 0:
            cleanup.append(self.browser_manager.close())
            cleanup.extend(server.stop() for server in getattr(getattr(self, "lsp_manager", None), "servers", {}).values())
            if getattr(self, "mcp_bridge", None):
                cleanup.append(self.mcp_bridge.cleanup())
        failures.extend(await asyncio.gather(*cleanup, return_exceptions=True))
        for failure in failures:
            if isinstance(failure, BaseException):
                raise failure
        if self.agent_depth == 0:
            self.session_manager.release_writer()

    async def __aenter__(self):
        self._assert_open()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def get_resume_summary(self) -> str:
        self.refresh_memory_files()
        metadata = self._build_session_metadata()
        plan = metadata.get("plan", {})
        todos = metadata.get("todos", [])
        sandbox = metadata.get("sandbox", {})
        browser = metadata.get("browser", {})
        subagents = metadata.get("subagents", [])
        shell_tasks = metadata.get("shell_tasks", [])
        loop_transitions = metadata.get("loop_transitions", [])
        post_sampling_history = metadata.get("post_sampling_history", [])
        permissions = metadata.get("permissions", {})
        pending_permission = permissions.get("pending_request") or {}
        memory_files = metadata.get("memory_files", {})
        file_state_cache = metadata.get("file_state_cache", {})
        artifacts = metadata.get("artifacts", {})
        commit_history = artifacts.get("commit_history", []) or []
        incremental_write_queue = metadata.get("incremental_write_queue", {})
        auto_commit = metadata.get("auto_commit", {})
        content_replacements = metadata.get("content_replacements", {})
        structured_output = metadata.get("structured_output", {})
        security_classifier = metadata.get("security_classifier", {})
        vcr = metadata.get("vcr", {})
        thinking = metadata.get("thinking", {})
        session_token_usage = metadata.get("session_token_usage", {})
        stats = metadata.get("message_statistics", {})
        effective_resume_thinking = thinking.get(
            "mode_overrides",
            {},
        ).get(
            plan.get("mode", "normal"),
            thinking.get("default", self.default_thinking_config),
        )
        artifact_file_change_count = sum(
            len(items) for items in dict(artifacts.get("file_history", {}) or {}).values()
        )

        lines = [
            f"Session ID: {self.session_id}",
            f"Mode: {plan.get('mode', 'normal')}",
            f"Model provider: {self.model_provider}",
            f"Primary model: {self.primary_model}",
            f"Fallback model: {self.fallback_model}",
            f"Thinking config: {self._thinking_config_to_text(effective_resume_thinking)}",
            f"Token estimator: {self.compactor.token_estimator_mode()}",
            f"Local tokenizer path: {self.local_tokenizer_path or 'none'}",
            f"Memory files loaded: {len(memory_files.get('loaded_files', []) or [])}",
            f"Cached file states: {len(file_state_cache.get('entries', {}) or {})}",
            f"Tracked attachments: {len(artifacts.get('attachments', []) or [])}",
            f"Tracked prefetches: {len(artifacts.get('prefetch_history', []) or [])}",
            f"Tracked file changes: {artifact_file_change_count}",
            f"Tracked commits: {len(commit_history)}",
            f"Incremental write queue events: {len(incremental_write_queue.get('history', []) or [])}",
            f"Auto-commit proposals tracked: {len(auto_commit.get('proposal_history', []) or [])}",
            f"Content replacements tracked: {len(content_replacements.get('registry', {}) or {})}",
            f"Structured output active: {'yes' if structured_output.get('request') else 'no'}",
            f"Post-sampling reports: {len(post_sampling_history)}",
            f"Security classifier mode: {security_classifier.get('mode', 'off')}",
            f"Security classifier hits: {len(security_classifier.get('history', []) or [])}",
            f"VCR mode: {vcr.get('mode', 'off')}",
            f"VCR cassettes touched: {len(vcr.get('history', []) or [])}",
            f"Session cache creation tokens: {session_token_usage.get('cache_creation_input_tokens', 0)}",
            f"Session cache read tokens: {session_token_usage.get('cache_read_input_tokens', 0)}",
            f"Messages loaded: {len(self.messages)}",
            f"Todos: {len(todos)}",
            f"Subagents tracked: {len(subagents)}",
            f"Shell tasks tracked: {len(shell_tasks)}",
            f"Loop transitions tracked: {len(loop_transitions)}",
            f"Permission denials tracked: {len(permissions.get('denial_history', []) or [])}",
            f"Pending permission request: {'yes' if pending_permission else 'no'}",
            f"Pending auto-commit proposal: {'yes' if auto_commit.get('pending_proposal') else 'no'}",
            f"Session input tokens: {session_token_usage.get('input_tokens', 0)}",
            f"Session output tokens: {session_token_usage.get('output_tokens', 0)}",
            f"Remaining output budget: {max(0, 64000 - int(session_token_usage.get('output_tokens', 0) or 0))}",
            f"Persistence status: {'ok' if self.last_persist_ok else 'error'}",
        ]
        if sandbox:
            lines.append(
                f"Active sandbox: {sandbox.get('branch')} @ {sandbox.get('path')}"
            )
        else:
            lines.append("Active sandbox: none")

        lines.append(
            f"Browser last URL: {browser.get('last_url') or 'none'}"
        )
        lines.append(
            f"Browser available: {'yes' if browser.get('available') else 'no'}"
        )
        if pending_permission:
            lines.append(
                "Recovered pending permission: "
                f"{pending_permission.get('tool_name')} "
                f"(risk={pending_permission.get('risk_level')}, mode={pending_permission.get('mode')})"
            )
        if commit_history:
            last_commit = commit_history[0]
            lines.append(
                "Recovered last commit attribution: "
                f"{last_commit.get('status')} "
                f"{last_commit.get('commit_hash') or '<none>'} "
                f"on {last_commit.get('branch') or '<detached>'} "
                f"via {last_commit.get('agent_role') or 'agent'}"
            )
        pending_auto_commit = auto_commit.get("pending_proposal") or {}
        if pending_auto_commit:
            lines.append(
                "Recovered pending auto-commit: "
                f"{pending_auto_commit.get('branch') or '<detached>'} "
                f"{pending_auto_commit.get('message_preview') or '<no message>'}"
            )

        lines.append(
            "Message stats: "
            f"user={stats.get('user_messages', 0)}, "
            f"assistant={stats.get('assistant_messages', 0)}, "
            f"tool_results={stats.get('tool_result_blocks', 0)}, "
            f"diagnostics={stats.get('system_observations', 0)}"
        )
        if self.last_persist_error:
            lines.append(f"Last persistence error: {self.last_persist_error}")
        return "\n".join(lines)

    def _run_hooks(self, event: str, payload: dict) -> list:
        try:
            return self.hook_manager.execute(event, payload)
        except Exception:
            return []

    def _append_hook_messages(self, event: str, outputs: list):
        if not outputs:
            return
        for index, content in enumerate(outputs, start=1):
            self.messages.append(create_user_message(
                f"<system-reminder>\n"
                f"[Hook:{event} #{index}]\n"
                f"{content}\n"
                f"</system-reminder>",
                is_meta=True,
                origin="hook",
            ))
        self._sync_message_context()

    def _rollback_incomplete_tool_turn(self, assistant_message: dict) -> bool:
        if not assistant_message or not self.messages:
            return False

        target_uuid = get_msg_uuid(assistant_message)
        if target_uuid:
            from xuloom.core.messages import rollback_to_uuid
            removed = rollback_to_uuid(self.messages, target_uuid)
            if removed:
                self._sync_message_context()
                self.persist_session_state()
                return True

        if self.messages and self.messages[-1] is assistant_message:
            content = assistant_message.get("content")
            if isinstance(content, list):
                has_tool_use = any(
                    (block.get("type") if isinstance(block, dict) else getattr(block, "type", None)) == "tool_use"
                    for block in content
                )
                if has_tool_use:
                    self.messages.pop()
                    self._sync_message_context()
                    self.persist_session_state()
                    return True

        removed = rollback_incomplete_turn(self.messages)
        if removed:
            self._sync_message_context()
            self.persist_session_state()
            return True

        return False

    async def _emit_stream_event(self, event_callback, payload: dict):
        if not callable(event_callback):
            return

        result = event_callback(payload)
        if inspect.isawaitable(result):
            await result

    def _serialize_response_usage(self, response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        fields = [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]
        payload = {}
        for field in fields:
            value = getattr(usage, field, None)
            if value is not None:
                payload[field] = value
        return payload

    def _serialize_response_for_vcr(self, response) -> dict:
        return {
            "stop_reason": getattr(response, "stop_reason", None),
            "content": [
                block
                for block in [self._block_to_dict(item) for item in getattr(response, "content", []) or []]
                if block
            ],
            "usage": self._serialize_response_usage(response),
        }

    async def _replay_vcr_response_events(self, response, *, turn_count: int, event_callback=None):
        await self._emit_stream_event(event_callback, {
            "type": "message_start",
            "turn": turn_count,
            "replayed": True,
        })
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")
                if text:
                    await self._emit_stream_event(event_callback, {
                        "type": "text_delta",
                        "turn": turn_count,
                        "text": text,
                        "replayed": True,
                    })
            elif getattr(block, "type", "") == "tool_use":
                await self._emit_stream_event(event_callback, {
                    "type": "tool_scheduled",
                    "turn": turn_count,
                    "tool_name": getattr(block, "name", ""),
                    "tool_use_id": getattr(block, "id", None),
                    "replayed": True,
                })
        await self._emit_stream_event(event_callback, {
            "type": "message_stop",
            "turn": turn_count,
            "stop_reason": getattr(response, "stop_reason", None),
            "replayed": True,
        })

    def _get_model_max_output_tokens(self, model: str = "") -> tuple:
        from xuloom.core.config import get_model_max_output_tokens
        return get_model_max_output_tokens(model or self.primary_model)

    def _get_max_output_tokens(self) -> int:
        """
        Resolve max output tokens: explicit env override, delegated response cap,
        then the ordinary conversation default. Reservations still enforce all
        remaining budgets before a delegated request reaches the provider.
        """
        from xuloom.core.config import DEFAULT_CAPPED_MAX_TOKENS
        env_val = os.environ.get("XULOOM_MAX_OUTPUT_TOKENS", "")
        if env_val:
            try:
                parsed = int(env_val)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
        default_tokens, _ = self._get_model_max_output_tokens()
        from xuloom.agents.collaboration import DELEGATION_BUDGET
        delegation = DELEGATION_BUDGET.get()
        if delegation is not None:
            return min(default_tokens, delegation["request_cap"])
        return min(default_tokens, DEFAULT_CAPPED_MAX_TOKENS)

    def _build_loop_state(self) -> dict:
        env_max = os.environ.get("XULOOM_MAX_TURNS", "")
        max_turns = int(env_max) if env_max.isdigit() else 1000
        if hasattr(self, "_max_turns_override") and self._max_turns_override:
            max_turns = self._max_turns_override
        output_budget = 200000
        if hasattr(self, "_output_budget_override") and self._output_budget_override:
            output_budget = self._output_budget_override
        return {
            "max_turns": max_turns,
            "turn_count": 0,
            "max_tokens": self._get_max_output_tokens(),
            "active_model": self.primary_model,
            "fallback_count": 0,
            "session_output_budget": output_budget,
            "session_input_budget": output_budget,
            "turn_input_tokens": 0,
            "turn_output_tokens": 0,
            "max_tokens_recovery_attempts": 0,
            "max_tokens_escalations": 0,
            "reactive_compact_retries": 0,
            "stop_hook_retries": 0,
            "structured_output_retries": 0,
            "continue_reason": "initial_turn",
        }

    async def _record_loop_transition(self, transition_reason: str, *, turn: int, event_callback=None, **details):
        record = {
            "reason": transition_reason,
            "turn": turn,
            **details,
        }
        self.loop_transition_history.insert(0, record)
        self.loop_transition_history = self.loop_transition_history[:20]
        self.tool_context["loop_transition_history"] = self.loop_transition_history
        await self._emit_stream_event(event_callback, {
            "type": "loop_transition",
            **record,
        })

    def _build_continuation_prompt(self) -> str:
        return (
            "<system-reminder>\n"
            "The previous assistant response stopped because the output token budget was exhausted.\n"
            "Continue immediately from where you left off. Do not repeat completed content.\n"
            "</system-reminder>"
        )




    def _build_stop_hook_feedback(self, reason: str) -> str:
        return (
            "<system-reminder>\n"
            "Stop hook blocked the previous answer.\n"
            "Revise the answer and try again.\n"
            f"Blocking reason:\n{reason}\n"
            "</system-reminder>"
        )

    def _record_token_usage(
        self,
        response,
        loop_state: dict,
        turn: int,
        *,
        system_prompt: str = "",
        api_messages: list = None,
        tools_schema: list = None,
        thinking_config: dict = None,
    ):
        usage = getattr(response, "usage", None)
        turn_record = {
            "turn": turn,
            "model": loop_state.get("active_model"),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0,
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0,
            "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage is not None else 0,
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage is not None else 0,
        }
        source = "api_usage"
        if turn_record["input_tokens"] <= 0 and turn_record["output_tokens"] <= 0:
            turn_record["input_tokens"] = self.compactor.estimate_request_tokens(
                api_messages or [],
                system=system_prompt,
                tools=tools_schema,
                thinking=thinking_config,
            )
            turn_record["output_tokens"] = self.compactor.estimate_content_tokens(
                "assistant",
                getattr(response, "content", []),
            )
            source = self.compactor._local_estimator_mode()
        turn_record["source"] = source

        self.token_usage_history.insert(0, turn_record)
        self.token_usage_history = self.token_usage_history[:50]
        self.tool_context["token_usage_history"] = self.token_usage_history

        for key in [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]:
            self.session_token_usage[key] = int(self.session_token_usage.get(key, 0) or 0) + turn_record[key]

        loop_state["turn_input_tokens"] = turn_record["input_tokens"]
        loop_state["turn_output_tokens"] = turn_record["output_tokens"]

    def _remaining_output_budget(self, loop_state: dict) -> int:
        from xuloom.core.output_limits import active_budgets
        from xuloom.features.goals import available_output
        budgets = active_budgets(self.tool_context.get("goal_budget"))
        if budgets:
            return min(available_output(budget) for budget in budgets)
        remaining = max(
            0,
            int(loop_state.get("session_output_budget", 0) or 0)
            - int(self.session_token_usage.get("output_tokens", 0) or 0),
        )
        return remaining

    def _is_model_fallback_error(self, error: Exception) -> bool:
        if isinstance(error, (APIConnectionError, RateLimitError, InternalServerError)):
            return True

        if isinstance(error, APIStatusError):
            status_code = getattr(error, "status_code", None)
            if status_code in {500, 502, 503, 504, 529}:
                return True

        rendered = str(error).lower()
        markers = [
            "overloaded",
            "service unavailable",
            "temporarily unavailable",
            "rate limit",
            "connection error",
        ]
        return any(marker in rendered for marker in markers)

    async def _await_gate_and_execute_tool(
        self, gate, tool_block, sys_print_callback=print, *,
        turn: int = 0, event_callback=None,
        semaphore: asyncio.Semaphore = None,
        sibling_cancel: asyncio.Event = None,
    ):
        if gate is not None:
            await gate
        if self.abort_event.is_set():
            raise AbortRequestedError("Abort requested before tool execution.")
        if sibling_cancel is not None and sibling_cancel.is_set():
            tool_id = getattr(tool_block, "id", "") or (tool_block.get("id", "") if isinstance(tool_block, dict) else "")
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "Cancelled: sibling tool in batch failed.",
                "is_error": True,
            }

        async def _do_execute():
            res = await self._execute_tool_wrapper(
                tool_block, sys_print_callback, turn=turn, event_callback=event_callback,
            )
            if (
                res.get("is_error")
                and sibling_cancel is not None
                and (getattr(tool_block, "name", "") or (tool_block.get("name", "") if isinstance(tool_block, dict) else ""))
                in self.SIBLING_CANCEL_TOOLS
            ):
                sibling_cancel.set()
            return res

        if semaphore is not None:
            async with semaphore:
                return await _do_execute()
        return await _do_execute()

    async def _cancel_streaming_tool_tasks(self, entries: list):
        pending = [
            item.get("task")
            for item in entries or []
            if item.get("task") is not None and not item["task"].done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _stream_model_response(
        self, **kwargs,
    ):
        if self.tool_context.get("external_backend"):
            from xuloom.agents.external.claude import sample
            return await sample(self, **kwargs)
        from xuloom.core.output_limits import model_reservation
        async with model_reservation(self, kwargs["max_tokens"]) as grant:
            kwargs["max_tokens"] = grant["maximum"]
            response, entries, fallback = await self._receive_model_response(**kwargs)
            grant["reported"] = int(self._serialize_response_usage(response).get("output_tokens", 0) or 0)
            return response, entries, fallback

    async def _receive_model_response(
        self,
        *,
        system_prompt: str,
        api_messages: list,
        tools_schema: list,
        turn_count: int,
        model_name: str,
        max_tokens: int,
        thinking_config: dict = None,
        sys_print_callback=print,
        event_callback=None,
    ):
        """Returns (response, tool_entries, streaming_fallback_occurred)."""
        from xuloom.core.model_inputs import validate_media, validate_transport
        validate_media(api_messages, self._input_contract(model_name))
        validate_transport(api_messages, self.model_provider)
        self.tool_context["request_model"] = model_name
        tool_entries = []
        self.active_streaming_tool_entries = tool_entries
        previous_batch_gate = None
        current_safe_batch_gate = None
        current_safe_batch_tasks = None
        streaming_semaphore = asyncio.Semaphore(self.MAX_TOOL_CONCURRENCY)
        streaming_sibling_cancel = asyncio.Event()
        openai_watch = None

        try:
            if self.model_provider == "openai":
                openai_messages = self._clean_roles_for_openai_api(
                    api_messages,
                    system_prompt,
                )
                create_kwargs = {
                    "model": model_name,
                    "messages": openai_messages,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tools_schema:
                    create_kwargs["tools"] = tools_schema
                profile = self.reasoning_profile if model_name == self.primary_model else configured_profile(self.model_provider, model_name, self.api_base_url)
                if profile is not None:
                    create_kwargs.update(request_options(profile, self.model_provider,
                        effort_override=self.reasoning_effort if model_name == self.primary_model else None, max_tokens=max_tokens))
                elif self.reasoning_effort and model_name == self.primary_model:
                    create_kwargs["reasoning_effort"] = self.reasoning_effort

                text_fragments = []
                text_filter = ToolTextFilter()
                tool_calls = {}
                dispatched_indices = set()
                emitted_tool_ids = set()
                finish_reason = "end_turn"
                usage_payload = self._make_usage_block()

                openai_sibling_cancel = asyncio.Event()

                def _dispatch_tool_if_ready(idx):
                    """Dispatch a tool call as soon as its arguments are fully received."""
                    if idx in dispatched_indices:
                        return
                    record = tool_calls.get(idx)
                    if not record or not record["id"] or not record["name"]:
                        return
                    dispatched_indices.add(idx)

                    tool_id = record["id"]
                    raw_args = "".join(record.get("arguments", []) or [])
                    argument_error = ""
                    try:
                        parsed_args = parse_tool_arguments(raw_args)
                    except ToolProtocolError as exc:
                        parsed_args, argument_error = {}, str(exc)

                    tool_block = SimpleNamespace(
                        type="tool_use",
                        id=tool_id,
                        name=record["name"],
                        input=parsed_args,
                        _argument_error=argument_error,
                        _raw_arguments=raw_args,
                    )

                    is_safe = self._is_concurrency_safe(record["name"], parsed_args)
                    nonlocal previous_batch_gate, current_safe_batch_tasks, current_safe_batch_gate

                    if is_safe:
                        if current_safe_batch_tasks is None:
                            current_safe_batch_gate = previous_batch_gate
                            current_safe_batch_tasks = []
                        task = asyncio.create_task(
                            self._await_gate_and_execute_tool(
                                current_safe_batch_gate,
                                tool_block,
                                sys_print_callback,
                                turn=turn_count,
                                event_callback=event_callback,
                                semaphore=streaming_semaphore,
                                sibling_cancel=openai_sibling_cancel,
                            )
                        )
                        current_safe_batch_tasks.append(task)
                    else:
                        if current_safe_batch_tasks is not None:
                            previous_batch_gate = asyncio.gather(*current_safe_batch_tasks)
                            current_safe_batch_tasks = None
                            current_safe_batch_gate = None
                        task = asyncio.create_task(
                            self._await_gate_and_execute_tool(
                                previous_batch_gate,
                                tool_block,
                                sys_print_callback,
                                turn=turn_count,
                                event_callback=event_callback,
                                semaphore=streaming_semaphore,
                            )
                        )
                        previous_batch_gate = task

                    tool_entries.append({
                        "tool_use_id": tool_id,
                        "task": task,
                    })

                async def stream_status(metrics):
                    await self._emit_stream_event(event_callback, {"type": "stream_status", "turn": turn_count, "metrics": metrics})
                openai_watch = StreamWatch(self.abort_event, stream_status)
                await openai_watch.status("waiting", force=True)
                stream = await openai_watch.wait(self.client.chat.completions.create(**create_kwargs))
                self.active_stream = stream
                openai_watch.metrics["connectedMs"] = openai_watch.ms()
                await openai_watch.status("connected", force=True)
                await self._emit_stream_event(event_callback, {
                    "type": "message_start",
                    "turn": turn_count,
                })

                last_tool_index = -1

                iterator = stream.__aiter__()
                while True:
                    try:
                        chunk = await openai_watch.wait(iterator.__anext__())
                    except StopAsyncIteration:
                        break
                    except (StreamAborted, StreamInterrupted):
                        raise
                    except Exception as exc:
                        raise StreamInterrupted("模型流传输中断，已收到的内容保留。请检查后继续，不会自动重放工具操作。") from exc
                    await openai_watch.chunk(chunk)
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested during OpenAI streaming response.")
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage_payload = self._make_usage_block(
                            input_tokens=getattr(chunk_usage, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(chunk_usage, "completion_tokens", 0) or 0,
                        )

                    choices = list(getattr(chunk, "choices", []) or [])
                    if not choices:
                        if openai_watch.terminal_at is not None and chunk_usage is not None:
                            break
                        continue

                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        delta_text = getattr(delta, "content", None)
                        if delta_text:
                            text_fragments.append(delta_text)
                            visible_delta = text_filter.feed(delta_text)
                            if visible_delta:
                                await self._emit_stream_event(event_callback, {"type": "text_delta", "turn": turn_count, "text": visible_delta})
                                await openai_watch.visible()

                        for tool_delta in list(getattr(delta, "tool_calls", []) or []):
                            index = int(getattr(tool_delta, "index", 0) or 0)

                            if index > last_tool_index and last_tool_index >= 0:
                                _dispatch_tool_if_ready(last_tool_index)
                            last_tool_index = max(last_tool_index, index)

                            record = tool_calls.setdefault(index, {
                                "id": "",
                                "name": "",
                                "arguments": [],
                            })
                            tool_id = getattr(tool_delta, "id", None)
                            if tool_id:
                                record["id"] = tool_id
                            fn = getattr(tool_delta, "function", None)
                            if fn is not None:
                                fn_name = getattr(fn, "name", None)
                                fn_arguments = getattr(fn, "arguments", None)
                                if fn_name:
                                    record["name"] = fn_name
                                if fn_arguments:
                                    record["arguments"].append(fn_arguments)
                            if record["id"] and record["name"] and record["id"] not in emitted_tool_ids:
                                emitted_tool_ids.add(record["id"])
                                await self._emit_stream_event(event_callback, {
                                    "type": "tool_scheduled",
                                    "turn": turn_count,
                                    "tool_name": record["name"],
                                    "tool_use_id": record["id"],
                                })

                    if getattr(choice, "finish_reason", None):
                        finish_reason = self._map_openai_finish_reason(choice.finish_reason)
                    if openai_watch.terminal_at is not None and chunk_usage is not None:
                        break

                if openai_watch.terminal_at is None:
                    raise StreamInterrupted("模型连接在收到结束标记前断开，已有内容可能不完整，已保留。请检查后继续，不会自动重放工具操作。")
                await openai_watch.status("completed", force=True)

                full_text = "".join(text_fragments)
                if not tool_calls:
                    try:
                        full_text, recovered = decode_tool_text(full_text, tools_schema)
                    except ToolProtocolError as exc:
                        exc.usage = usage_payload
                        raise
                    for index, call in enumerate(recovered):
                        tool_calls[index] = {**call, "arguments": [call["arguments"]]}
                else:
                    # Native calls take precedence over a duplicate leaked envelope.
                    from xuloom.protocols.dsml import marker_position
                    position = marker_position(full_text)
                    if position is not None:
                        full_text = full_text[:position]
                tail = text_filter.finish(full_text)
                if tail:
                    await self._emit_stream_event(event_callback, {"type": "text_delta", "turn": turn_count, "text": tail})
                if tool_calls:
                    finish_reason = "tool_use"
                for idx in sorted(tool_calls.keys()):
                    _dispatch_tool_if_ready(idx)

                content_blocks = []
                if full_text:
                    content_blocks.append(self._make_text_block(full_text))
                for record in [tool_calls[idx] for idx in sorted(tool_calls.keys())]:
                    tool_id = record.get("id") or f"call_{len(content_blocks) + 1}"
                    raw_arguments = "".join(record.get("arguments", []) or [])
                    argument_error = ""
                    try:
                        parsed_arguments = parse_tool_arguments(raw_arguments)
                    except ToolProtocolError as exc:
                        parsed_arguments, argument_error = {}, str(exc)
                    block = self._make_tool_use_block(
                            tool_id=tool_id,
                            name=record.get("name", ""),
                            input_payload=parsed_arguments,
                        )
                    block._argument_error = argument_error
                    block._raw_arguments = raw_arguments
                    content_blocks.append(block)

                await self._emit_stream_event(event_callback, {
                    "type": "message_stop",
                    "turn": turn_count,
                    "stop_reason": finish_reason,
                })
                return SimpleNamespace(
                    stop_reason=finish_reason,
                    content=content_blocks,
                    usage=usage_payload,
                ), tool_entries, False

            system_blocks = self._build_cached_system_blocks(system_prompt)
            stream_kwargs = {
                "model": model_name,
                "system": system_blocks,
                "messages": api_messages,
                "tools": tools_schema,
                "max_tokens": max_tokens,
            }
            profile = self.reasoning_profile if model_name == self.primary_model else configured_profile(self.model_provider, model_name, self.api_base_url)
            if profile is not None:
                stream_kwargs.update(request_options(profile, self.model_provider,
                    effort_override=self.reasoning_effort if model_name == self.primary_model else None, max_tokens=max_tokens))
            elif thinking_config is not None and self._thinking_config_explicit and self._is_native_anthropic():
                stream_kwargs["thinking"] = thinking_config

            try:
                async with self.client.messages.stream(**stream_kwargs) as stream:
                    self.active_stream = stream
                    while True:
                        next_event_task = asyncio.create_task(stream.__anext__())
                        abort_wait_task = asyncio.create_task(self.abort_event.wait())
                        done, pending = await asyncio.wait(
                            {next_event_task, abort_wait_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)

                        if abort_wait_task in done and self.abort_event.is_set():
                            next_event_task.cancel()
                            await asyncio.gather(next_event_task, return_exceptions=True)
                            raise AbortRequestedError("Abort requested during streaming response.")

                        try:
                            event = next_event_task.result()
                        except StopAsyncIteration:
                            break

                        event_type = getattr(event, "type", "")
                        if event_type == "message_start":
                            await self._emit_stream_event(event_callback, {
                                "type": "message_start",
                                "turn": turn_count,
                            })
                        elif event_type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if getattr(delta, "type", "") == "text_delta":
                                await self._emit_stream_event(event_callback, {
                                    "type": "text_delta",
                                    "turn": turn_count,
                                    "text": getattr(delta, "text", ""),
                                })
                        elif event_type == "content_block_stop":
                            content_block = getattr(event, "content_block", None)
                            if getattr(content_block, "type", "") == "tool_use":
                                is_safe = self._is_concurrency_safe(
                                    getattr(content_block, "name", ""),
                                    getattr(content_block, "input", {}) or {},
                                )
                                if is_safe:
                                    if current_safe_batch_tasks is None:
                                        current_safe_batch_gate = previous_batch_gate
                                        current_safe_batch_tasks = []
                                    task = asyncio.create_task(
                                        self._await_gate_and_execute_tool(
                                            current_safe_batch_gate,
                                            content_block,
                                            sys_print_callback,
                                            turn=turn_count,
                                            event_callback=event_callback,
                                            semaphore=streaming_semaphore,
                                            sibling_cancel=streaming_sibling_cancel,
                                        )
                                    )
                                    current_safe_batch_tasks.append(task)
                                else:
                                    if current_safe_batch_tasks is not None:
                                        previous_batch_gate = asyncio.gather(*current_safe_batch_tasks)
                                        current_safe_batch_tasks = None
                                        current_safe_batch_gate = None
                                    task = asyncio.create_task(
                                        self._await_gate_and_execute_tool(
                                            previous_batch_gate,
                                            content_block,
                                            sys_print_callback,
                                            turn=turn_count,
                                            event_callback=event_callback,
                                            semaphore=streaming_semaphore,
                                        )
                                    )
                                    previous_batch_gate = task

                                tool_entries.append({
                                    "tool_use_id": getattr(content_block, "id", None),
                                    "task": task,
                                })
                                await self._emit_stream_event(event_callback, {
                                    "type": "tool_scheduled",
                                    "turn": turn_count,
                                    "tool_name": getattr(content_block, "name", ""),
                                    "tool_use_id": getattr(content_block, "id", None),
                                })

                            await self._emit_stream_event(event_callback, {
                                "type": "content_block_stop",
                                "turn": turn_count,
                                "index": getattr(event, "index", None),
                            })
                        elif event_type == "message_stop":
                            snapshot = stream.current_message_snapshot
                            await self._emit_stream_event(event_callback, {
                                "type": "message_stop",
                                "turn": turn_count,
                                "stop_reason": getattr(snapshot, "stop_reason", None),
                            })

                    final_message = await stream.get_final_message()
                    return final_message, tool_entries, False
            except AbortRequestedError:
                raise
            except Exception as stream_err:
                if self._is_context_overflow_error(stream_err):
                    raise
                await self._cancel_streaming_tool_tasks(tool_entries)
                tool_entries.clear()
                previous_batch_gate = None
                current_safe_batch_gate = None
                current_safe_batch_tasks = None

                disable_fallback = os.environ.get(
                    "XULOOM_DISABLE_NONSTREAMING_FALLBACK", ""
                )
                if disable_fallback:
                    raise

                sys_print_callback(
                    "[dim yellow]↳ Streaming request failed mid-stream; "
                    "retrying as non-streaming request.[/dim yellow]"
                )
                create_kwargs = {
                    k: v for k, v in stream_kwargs.items()
                }
                response = await self.client.messages.create(**create_kwargs)
                await self._emit_stream_event(event_callback, {
                    "type": "message_start",
                    "turn": turn_count,
                })
                for block in response.content:
                    btype = getattr(block, "type", "")
                    if btype == "text":
                        await self._emit_stream_event(event_callback, {
                            "type": "text_delta",
                            "turn": turn_count,
                            "text": getattr(block, "text", ""),
                        })
                    elif btype == "tool_use":
                        await self._emit_stream_event(event_callback, {
                            "type": "tool_scheduled",
                            "turn": turn_count,
                            "tool_name": getattr(block, "name", ""),
                            "tool_use_id": getattr(block, "id", None),
                        })
                await self._emit_stream_event(event_callback, {
                    "type": "message_stop",
                    "turn": turn_count,
                    "stop_reason": getattr(response, "stop_reason", None),
                })
                return response, [], True
        except StreamAborted:
            await self._cancel_streaming_tool_tasks(tool_entries)
            if openai_watch:
                await openai_watch.status("cancelled", force=True)
            raise AbortRequestedError("Abort requested while receiving model stream.")
        except StreamInterrupted as exc:
            exc.partial_text = text_filter.text[:text_filter.emitted] if self.model_provider == "openai" else ""
            await self._cancel_streaming_tool_tasks(tool_entries)
            exc.tool_results = [entry["task"].result() for entry in tool_entries
                                if entry["task"].done() and not entry["task"].cancelled() and entry["task"].exception() is None]
            if openai_watch:
                openai_watch.metrics["error"] = str(exc)
                await openai_watch.status("error", force=True)
            raise
        finally:
            if self.model_provider == "openai" and self.active_stream is not None:
                close = getattr(self.active_stream, "close", None) or getattr(self.active_stream, "aclose", None)
                if close:
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        pass
            self.active_stream = None
            self.active_streaming_tool_entries = []

    async def _collect_subagents_before_final(self, *, turn, event_callback=None):
        """Close the completion race without replaying work or consuming model turns while waiting."""
        workers = self.tool_context["subagent_tasks"]
        active = lambda: {key: task for key, task in workers.tasks.items() if not task.done()}
        pending = active()
        waited = bool(pending)
        if pending:
            stopped = asyncio.create_task(self.abort_event.wait())
            try:
                while pending:
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested while waiting for subagents.")
                    await self._emit_stream_event(event_callback, {"type": "subagents_waiting", "count": len(pending),
                        "agent_ids": list(pending), "message": f"等待 {len(pending)} 个子代理返回结果，全部结束后再汇总。"})
                    await asyncio.wait([*pending.values(), stopped], return_when=asyncio.FIRST_COMPLETED)
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested while waiting for subagents.")
                    pending = active()
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
        # Include workers that finished while the model was streaming its answer,
        # even if no worker remains active by the time this gate is reached.
        reports = workers.completed()
        if not waited and not reports:
            return False
        for report in reports:
            self.messages.append(create_user_message("Background subagent completed: " + json.dumps(report, ensure_ascii=False), origin="subagent"))
        self.messages.append(create_user_message(
            "\n\n[Delegation completion guard]\nThe previous draft was provisional. All owned subagent tasks have now ended, "
            "and any newly returned results are included above. Continue the requested workflow and synthesize the actual results. "
            "Distinguish completed, crashed, cancelled and missing-output workers. Do not invent their output or claim that all succeeded. "
            "Do not relaunch or repeat completed work just to prepare the final answer.",
            is_meta=True, origin="delegation_completion"))
        self._sync_message_context()
        await self._record_loop_transition("delegation_completion_guard", turn=turn, event_callback=event_callback,
            waited=waited, report_count=len(reports))
        await self._emit_stream_event(event_callback, {"type": "subagents_waiting", "count": 0,
            "message": "子代理均已结束，正在汇总实际结果；失败或取消的任务会单独说明。"})
        self.persist_session_state()
        return True

    async def _run_loop(self, user_input: str, sys_print_callback=print, event_callback=None, input_blocks=None, attachments=None, display_text=None, selected_skills=None, resume=False):
        """
        The Core Action Loop:
        1. Contextualize System Prompt.
        2. Feed user text to the LLM.
        3. If tool calls -> Execute in parallel via asyncio & Append results to loop.
        4. If stop -> Return final textual output.
        """
        if self.tool_context.get("delegation", {}).get("authority_required") and self.permission_manager.delegated_authorizer is None:
            return RunResult("This is an owned delegated execution. Resume it through its original controller, or fork an independent conversation.",
                "delegation_unavailable", session_id=self.session_id, agent_id=self.agent_id)
        try:
            self.require_writer()
        except (SessionBusyError, SessionConflictError) as error:
            return RunResult(str(error), "session_busy" if isinstance(error, SessionBusyError) else "session_conflict", session_id=self.session_id, agent_id=self.agent_id)
        if resume and (user_input or input_blocks or attachments or selected_skills):
            raise ValueError("Resume cannot replace the original prompt, attachments or skills")
        from xuloom.core.model_inputs import validate_media, validate_transport, UnsupportedInput
        try:
            input_history = self.messages + ([{"content": input_blocks}] if input_blocks else [])
            validate_transport(input_history, self.model_provider)
            validate_media(input_history,
                self._input_contract(self.primary_model))
        except UnsupportedInput as error:
            return finish_run(self, str(error), 'unsupported_input')
        if self.agent_depth == 0 and not resume:
            from xuloom.agents.collaboration import new_run, policy
            profile = policy(self.collaboration_mode, self.tool_context["skill_registry"].settings())
            self.tool_context["collaboration"] = profile
            self.tool_context["collaboration_run"] = new_run(profile)
        self.abort_event.clear()
        completion = self.tool_context["workflow_completion"]
        if not resume:
            completion.reset()
        registry = self.tool_context["skill_registry"]
        names = list(dict.fromkeys(selected_skills or []))
        # CLI mentions and desktop selections use the same loader and stored input.
        for mention in re.finditer(r"(?:^|\s)\$([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?=\s|$)", user_input):
            name = mention.group(1)
            if name not in names:
                names.append(name)
        selections, seen = [], set()
        for name in names:
            skill = registry.resolve(name)
            if skill["id"] not in seen:
                selections.append((skill, registry.read(skill["name"])))
                seen.add(skill["id"])
        if selections:
            display_text = user_input if display_text is None else display_text
            instructions = "\n\n<selected-skills>\n" + "\n\n".join(body for _, body in selections) + "\n</selected-skills>"
            if input_blocks is None:
                user_input += instructions
            else:
                input_blocks = copy.deepcopy(input_blocks) + [{"type": "text", "text": instructions}]
            for skill, _ in selections:
                completion.activate(skill)
                loaded = self.tool_context.setdefault("loaded_skills", [])
                if skill["name"] not in loaded:
                    loaded.append(skill["name"])
                await self._emit_stream_event(event_callback, {"type": "skill_loaded", "name": skill["name"]})

        if not self.is_configured:
            return finish_run(self, ("Model not configured. Use /model to set protocol, model name, "
                "base URL, and API key before sending messages."), 'not_configured')

        if resume:
            user_msg = next((message for message in reversed(self.messages) if message.get("role") == "user"
                and message.get("_origin", "human") == "human" and not message.get("_is_meta")
                and not message.get("_is_tool_result") and not any(block.get("type") == "tool_result"
                    for block in message.get("content", []) if isinstance(block, dict))), None)
            if user_msg is None:
                raise ValueError("There is no original user task to resume")
            # Use the existing recovery receipts, never replay uncertain tools.
            restore_tool_activity(self, {"tool_activity": {"in_progress": self.in_progress_tools,
                "recent": self.recent_tool_activity}})
            self.messages.append(create_user_message(
                "[Task resumed by user]\nContinue the original interrupted task using its existing instructions, "
                "attachments, partial response and saved tool results. Continue the unfinished response without "
                "repeating its existing text or adding a restart announcement. Do not repeat completed operations. "
                "Interrupted tools may have uncertain effects: inspect saved results/current state before any new action. "
                "Cancelled questions or approvals are not answers or authorization; ask again if still needed.",
                is_meta=True, is_continuation=True, origin="resume_control"))
        else:
            user_msg = create_user_message(input_blocks if input_blocks is not None else user_input, origin="human")
            if attachments:
                user_msg["_attachments"] = attachments
            if display_text is not None:
                user_msg["_display_text"] = display_text
            self.messages.append(user_msg)
        if self.active_turn_trace is not None:
            self.active_turn_trace["userMessageId"] = get_msg_uuid(user_msg)
        self._sync_message_context()

        if not hasattr(self, "session_started"):
            self.session_started = False
        if not self.session_started:
            session_start_outputs = self._run_hooks("SessionStart", {
                "cwd": self.session_cwd,
                "mode": self.get_mode(),
                "agent_id": self.agent_id,
                "agent_role": self.agent_role,
            })
            self._append_hook_messages("SessionStart", session_start_outputs)
            self.session_started = True
            self.persist_session_state()
        
        loop_state = self._build_loop_state()
        protocol_failures = 0
        protocol_retries = 0
        input_failures = 0

        while loop_state["turn_count"] < loop_state["max_turns"]:
            mailbox = self.tool_context.get("delegated_mailbox")
            if mailbox:
                incoming = mailbox()
                if incoming:
                    seen = {message.get("_delegated_message_id") for message in self.messages}
                    for message in incoming:
                        if message["id"] not in seen:
                            item = create_user_message("Parent message: " + message["text"], origin="subagent")
                            item["_delegated_message_id"] = message["id"]
                            self.messages.append(item)
                            seen.add(message["id"])
                    self._sync_message_context()
                    self.persist_session_state()
                    if self.session_manager.last_save_ok:
                        self.tool_context["ack_delegated_mailbox"]({m["id"] for m in incoming})
            if self.team_manager.is_active:
                while True:
                    team_message = await self.team_manager.receive_message(self.agent_id)
                    if team_message is None:
                        break
                    self.messages.append(create_user_message("Team message from {}: {}".format(team_message.from_name, team_message.text), origin="team"))
            for report in self.tool_context["subagent_tasks"].completed():
                self.messages.append(create_user_message("Background subagent completed: " + str(report), origin="subagent"))
            loop_state["turn_count"] += 1
            turn_count = loop_state["turn_count"]
            assistant_message = None
            streaming_tool_entries = []
            loop_state["turn_input_tokens"] = 0
            loop_state["turn_output_tokens"] = 0

            # Recompute tools schema every turn so mode changes
            # (enter_plan_mode / exit_plan_mode) take effect immediately.
            tools_schema = self._get_anthropic_tools_schema()

            try:
                if self.abort_event.is_set():
                    raise AbortRequestedError("Abort requested before turn start.")
                await self._apply_layered_compaction_if_needed(sys_print_callback)
                remaining_output_budget = self._remaining_output_budget(loop_state)
                if remaining_output_budget <= 0:
                    await self._record_loop_transition(
                        "session_output_budget_exhausted",
                        turn=turn_count,
                        event_callback=event_callback,
                        session_output_budget=loop_state["session_output_budget"],
                        output_tokens_used=self.session_token_usage.get("output_tokens", 0),
                    )
                    self.persist_session_state()
                    return finish_run(self, ("Session output token budget exhausted before another turn could start."), 'budget_exhausted')
                self._inject_turn_attachments(turn_count)
                # NOTE: FRC (Function Result Clearing) removed — Claude Code does
                # not blindly clear old tool results. Budget enforcement via
                # ContentReplacementManager handles oversized results instead.
                boundary_messages = self._get_messages_after_compact_boundary()
                self._apply_tool_result_budget(boundary_messages)
                api_messages = self._clean_roles_for_api(boundary_messages)
                if self.model_provider == "anthropic" and self._is_native_anthropic():
                    api_messages = self._apply_message_cache_control(api_messages)
                loaded_memory_files = self.refresh_memory_files()
                effective_thinking_config = self._resolve_thinking_config()
                system_prompt = self.context_builder.generate_system_prompt(
                    session_summary=self.plan_manager.render_prompt_summary(),
                    todo_summary=self.todo_manager.render_prompt_summary(),
                    memory_summary=self.get_memory_summary(),
                    structured_output_summary=self.structured_output_manager.render_prompt_summary(),
                    tool_prompt_summary=self._build_tool_prompt_summary(),
                    collaboration_summary=self._collaboration_instructions(),
                    mcp_instructions=self._build_mcp_instructions(),
                )
                system_prompt += self._get_coordinator_system_prompt_addon()
                await self._emit_stream_event(event_callback, {
                    "type": "memory_files",
                    "turn": turn_count,
                    "count": len(loaded_memory_files),
                    "files": [
                        {
                            "scope": item.get("scope"),
                            "path": item.get("path"),
                            "truncated": item.get("truncated", False),
                        }
                        for item in loaded_memory_files
                    ],
                })

                vcr_request_payload = self.vcr_manager.build_request_payload(
                    provider=self.model_provider,
                    model=loop_state["active_model"],
                    system=system_prompt,
                    messages=api_messages,
                    tools=tools_schema,
                    max_tokens=min(loop_state["max_tokens"], remaining_output_budget),
                    thinking=effective_thinking_config,
                    reasoning={"profile": self.reasoning_profile if loop_state["active_model"] == self.primary_model else configured_profile(self.model_provider, loop_state["active_model"], self.api_base_url),
                        "effort": self.reasoning_effort if loop_state["active_model"] == self.primary_model else None},
                )
                streaming_fallback = False
                replayed_response = self.vcr_manager.try_replay(vcr_request_payload)
                if replayed_response is not None:
                    response = replayed_response
                    streaming_tool_entries = []
                    await self._replay_vcr_response_events(
                        response,
                        turn_count=turn_count,
                        event_callback=event_callback,
                    )
                else:
                    response, streaming_tool_entries, streaming_fallback = await self._stream_model_response(
                        system_prompt=system_prompt,
                        api_messages=api_messages,
                        tools_schema=tools_schema,
                        turn_count=turn_count,
                        model_name=loop_state["active_model"],
                        max_tokens=min(loop_state["max_tokens"], remaining_output_budget),
                        thinking_config=effective_thinking_config,
                        sys_print_callback=sys_print_callback,
                        event_callback=event_callback,
                    )
                    if streaming_fallback:
                        await self._emit_stream_event(event_callback, {
                            "type": "streaming_fallback",
                            "turn": turn_count,
                        })
                    self.vcr_manager.record(
                        vcr_request_payload,
                        self._serialize_response_for_vcr(response),
                    )
                self._record_token_usage(
                    response,
                    loop_state,
                    turn_count,
                    system_prompt=system_prompt,
                    api_messages=api_messages,
                    tools_schema=tools_schema,
                    thinking_config=effective_thinking_config,
                )
                protocol_failures = 0
                await self._emit_stream_event(event_callback, {
                    "type": "token_usage",
                    "turn": turn_count,
                    "input_tokens": loop_state["turn_input_tokens"],
                    "output_tokens": loop_state["turn_output_tokens"],
                    "session_input_tokens": self.session_token_usage.get("input_tokens", 0),
                    "session_output_tokens": self.session_token_usage.get("output_tokens", 0),
                })
                await self._emit_stream_event(event_callback, {
                    "type": "thinking_config",
                    "turn": turn_count,
                    "config": effective_thinking_config,
                    "mode": self.get_mode(),
                })
                
                content_as_dicts = [self._block_to_dict(b) for b in response.content]
                content_as_dicts = [b for b in content_as_dicts if b is not None]
                assistant_message = create_assistant_message(
                    content_as_dicts,
                    model=loop_state.get("active_model", ""),
                    stop_reason=getattr(response, "stop_reason", ""),
                )
                self.messages.append(assistant_message)
                self._sync_message_context()
                # Checkpointing memory auto-flush
                self.persist_session_state()
                
                if response.stop_reason == "tool_use":
                    loop_state["workflow_guard_stalls"] = 0
                    if streaming_tool_entries:
                        tool_results = []
                        for item in streaming_tool_entries:
                            if self.abort_event.is_set():
                                raise AbortRequestedError("Abort requested while awaiting tool results.")
                            tool_results.append(await item["task"])
                    else:
                        tool_calls = [b for b in content_as_dicts if isinstance(b, dict) and b.get("type") == "tool_use"]
                        tool_results = []
                        results = await self._execute_tool_batches(
                            tool_calls,
                            sys_print_callback,
                            turn=turn_count,
                            event_callback=event_callback,
                        )
                        tool_results.extend(results)
                    
                    tool_result_msg = create_tool_result_message(
                        tool_results,
                        source_assistant_uuid=get_msg_uuid(assistant_message),
                    )
                    self.messages.append(tool_result_msg)
                    self._sync_message_context()
                    if any(item.get("_input_error") for item in tool_results):
                        input_failures += 1
                        retrying = input_failures <= 2 and protocol_retries < 6
                        message = (f"工具参数校验失败，正在请求模型修正（{input_failures}/2）。已完成的操作保留。" if retrying else
                                   "工具参数多次修正失败，任务已中断。已完成的操作和文件保留，可继续当前会话。")
                        await self._record_loop_transition("tool_input_repair" if retrying else "tool_input_exhausted",
                            turn=turn_count, event_callback=event_callback, attempt=input_failures, retrying=retrying)
                        await self._emit_stream_event(event_callback, {"type": "tool_protocol_error", "message": message,
                            "retrying": retrying, "detail": "Invalid tool input; see the rejected call's tool result."})
                        sys_print_callback(message)
                        self.persist_session_state()
                        if not retrying:
                            halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="tool_input_error")
                            halted["_is_meta"] = True
                            self.messages.append(halted)
                            self._sync_message_context()
                            self.persist_session_state()
                            return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + message), 'tool_input_error')
                        protocol_retries += 1
                    else:
                        input_failures = 0
                    
                    # Diagnostics exist only after a language-server tool was explicitly used.
                    if hasattr(self, 'lsp_manager'):
                        diags_msg = self.lsp_manager.consume_diagnostics()
                        if diags_msg:
                            self.messages.append(create_user_message(
                                f"[System Observation]\\n{diags_msg}",
                                is_meta=True,
                                origin="lsp_diagnostics",
                            ))
                            self._sync_message_context()
                            sys_print_callback("[bold red]⚠ Active Compiler Diagnostics intercepted and fed to agent loop![/bold red]")
                    
                    # Tool cycle complete, checkpoint again
                    self.persist_session_state()
                    loop_state["continue_reason"] = "next_turn"
                    await self._record_loop_transition(
                        "next_turn",
                        turn=turn_count,
                        event_callback=event_callback,
                        stop_reason="tool_use",
                    )
                    
                elif response.stop_reason == "max_tokens":
                    if streaming_tool_entries:
                        await self._cancel_streaming_tool_tasks(streaming_tool_entries)
                    loop_state["max_tokens_recovery_attempts"] += 1
                    remaining_output_budget = self._remaining_output_budget(loop_state)
                    if remaining_output_budget <= 0:
                        await self._record_loop_transition(
                            "session_output_budget_exhausted",
                            turn=turn_count,
                            event_callback=event_callback,
                            session_output_budget=loop_state["session_output_budget"],
                            output_tokens_used=self.session_token_usage.get("output_tokens", 0),
                        )
                        self.persist_session_state()
                        return finish_run(self, ("Session output token budget exhausted before continuation."), 'budget_exhausted')
                    transition_reason = "max_output_tokens_recovery"
                    if (
                        loop_state["max_tokens_recovery_attempts"] >= 2
                        and loop_state["max_tokens_escalations"] < 1
                    ):
                        loop_state["max_tokens"] = 64000
                        loop_state["max_tokens_escalations"] += 1
                        transition_reason = "max_output_tokens_escalate"
                    loop_state["max_tokens"] = min(loop_state["max_tokens"], remaining_output_budget)
                    self.messages.append(create_user_message(
                        self._build_continuation_prompt(),
                        is_continuation=True,
                        origin="system_continuation",
                    ))
                    self._sync_message_context()
                    self.persist_session_state()
                    await self._record_loop_transition(
                        transition_reason,
                        turn=turn_count,
                        event_callback=event_callback,
                        max_tokens=loop_state["max_tokens"],
                        recovery_attempts=loop_state["max_tokens_recovery_attempts"],
                        remaining_output_budget=remaining_output_budget,
                    )
                    sys_print_callback(
                        f"[dim yellow]↳ Continuing after max_tokens stop ({transition_reason}, budget={loop_state['max_tokens']}).[/dim yellow]"
                    )
                    continue
                else:
                    if streaming_tool_entries:
                        await self._cancel_streaming_tool_tasks(streaming_tool_entries)
                    # Normal completion without Tool calls => Return final answer
                    texts = [b.get("text", "") for b in content_as_dicts if isinstance(b, dict) and b.get("type") == "text"]
                    final_answer = "\n".join(texts)
                    if await self._collect_subagents_before_final(turn=turn_count, event_callback=event_callback):
                        assistant_message["_stop_reason"] = "awaiting_subagents"
                        self.persist_session_state()
                        continue

                    if completion.pending:
                        if self.abort_event.is_set():
                            raise AbortRequestedError("Abort requested before workflow completion check.")
                        stalls = loop_state.get("workflow_guard_stalls", 0) + 1
                        retries = loop_state.get("workflow_guard_retries", 0)
                        retrying = stalls <= 2 and retries < 6
                        loop_state["workflow_guard_stalls"] = stalls
                        assistant_message["_stop_reason"] = "workflow_incomplete"
                        message = (f"技能流程尚未完成，正在请求模型继续执行（{stalls}/2）。" if retrying else
                                   "技能流程尚未完成，模型多次提前结束，任务已中断。已有结果保留，可继续当前会话。")
                        await self._emit_stream_event(event_callback, {"type": "workflow_incomplete", "message": message, "retrying": retrying})
                        await self._record_loop_transition("workflow_completion_guard" if retrying else "workflow_completion_exhausted",
                            turn=turn_count, event_callback=event_callback, retrying=retrying, attempt=stalls)
                        if retrying:
                            loop_state["workflow_guard_retries"] = retries + 1
                            self.messages.append(create_user_message(completion.feedback(), is_meta=True, origin="workflow_completion"))
                        else:
                            halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="workflow_incomplete")
                            halted["_is_meta"] = True
                            self.messages.append(halted)
                        self._sync_message_context()
                        self.persist_session_state()
                        if retrying:
                            continue
                        return finish_run(self, ("[bold red]System Halt:[/bold red] " + message), 'workflow_incomplete')

                    # Plan mode guard: model ended with text-only output while plan mode
                    # is active. It should have called exit_plan_mode or ask_user_question_tool.
                    if (
                        self.get_mode() == "plan"
                        and loop_state.get("plan_mode_guard_retries", 0) < 2
                    ):
                        loop_state["plan_mode_guard_retries"] = loop_state.get("plan_mode_guard_retries", 0) + 1
                        pfp = self.plan_manager.plan_file_path
                        guard_msg = (
                            "<system-reminder>\n"
                            "You are in PLAN MODE. You must NOT answer the user's question directly.\n\n"
                            "Your turn MUST end with one of these tool calls:\n"
                            "  1. exit_plan_mode — when your plan is ready for approval\n"
                            "  2. ask_user_question_tool — to clarify requirements\n\n"
                            "Do NOT output a plain-text answer. Instead:\n"
                            f"  - Inspect relevant materials with read-only tools\n"
                            f"  - Write your plan to the plan file at {pfp}\n"
                            "  - Then call exit_plan_mode\n"
                            "</system-reminder>"
                        )
                        self.messages.append(create_user_message(
                            guard_msg, is_meta=True, origin="plan_mode_guard",
                        ))
                        self._sync_message_context()
                        self.persist_session_state()
                        await self._record_loop_transition(
                            "plan_mode_guard",
                            turn=turn_count,
                            event_callback=event_callback,
                            retries=loop_state["plan_mode_guard_retries"],
                        )
                        sys_print_callback(
                            "[dim yellow]↳ Plan mode guard: model ended without calling exit_plan_mode; injecting reminder.[/dim yellow]"
                        )
                        continue

                    structured_ok, structured_reason = self.structured_output_manager.validate(final_answer)
                    if not structured_ok:
                        loop_state["structured_output_retries"] += 1
                        self.messages.append(create_user_message(
                            self._build_structured_output_feedback(structured_reason),
                            is_meta=True,
                            origin="structured_output_retry",
                        ))
                        self._sync_message_context()
                        self.persist_session_state()
                        await self._record_loop_transition(
                            "structured_output_retry",
                            turn=turn_count,
                            event_callback=event_callback,
                            retries=loop_state["structured_output_retries"],
                            reason=structured_reason,
                        )
                        sys_print_callback(
                            "[dim yellow]↳ Structured output validation failed; retrying with feedback.[/dim yellow]"
                        )
                        continue
                    stop_hook_decision = await self.hook_manager.evaluate_stop_hooks_async({
                        "cwd": self.session_cwd,
                        "mode": self.get_mode(),
                        "agent_id": self.agent_id,
                        "agent_role": self.agent_role,
                        "final_answer": final_answer,
                        "turn_input_tokens": loop_state["turn_input_tokens"],
                        "turn_output_tokens": loop_state["turn_output_tokens"],
                        "session_token_usage": dict(self.session_token_usage),
                    })
                    if stop_hook_decision.behavior in {"block", "retry"}:
                        loop_state["stop_hook_retries"] += 1
                        self.messages.append(create_user_message(
                            self._build_stop_hook_feedback(
                                stop_hook_decision.reason or "Blocked by stop hook."
                            ),
                            is_meta=True,
                            origin="stop_hook",
                        ))
                        self._sync_message_context()
                        self.persist_session_state()
                        await self._record_loop_transition(
                            "stop_hook_blocking",
                            turn=turn_count,
                            event_callback=event_callback,
                            retries=loop_state["stop_hook_retries"],
                            behavior=stop_hook_decision.behavior,
                        )
                        sys_print_callback(
                            "[dim yellow]↳ StopHook blocked the current answer; retrying with feedback.[/dim yellow]"
                        )
                        continue
                    turn_end_outputs = self._run_hooks("TurnEnd", {
                        "cwd": self.session_cwd,
                        "mode": self.get_mode(),
                        "agent_id": self.agent_id,
                        "agent_role": self.agent_role,
                        "final_answer": final_answer,
                        "turn_input_tokens": loop_state["turn_input_tokens"],
                        "turn_output_tokens": loop_state["turn_output_tokens"],
                        "session_token_usage": dict(self.session_token_usage),
                    })
                    self._append_hook_messages("TurnEnd", turn_end_outputs)
                    await self._apply_post_sampling_hooks(
                        final_answer=final_answer,
                        turn=turn_count,
                        event_callback=event_callback,
                    )
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested before turn finalization.")
                    # Delegated runs own their transcript, not project-wide
                    # durable memories or untracked post-completion model jobs.
                    if not self.tool_context.get("delegation"):
                        estimated_tokens = self.compactor.estimate_tokens(self.messages)
                        await self.session_memory_manager.maybe_extract(
                            self.messages, estimated_tokens,
                        )
                        self.memory_extractor.record_turn()
                        await self.memory_extractor.maybe_extract(self.messages)
                    self.persist_session_state()
                    mailbox = self.tool_context.get("delegated_mailbox")
                    if mailbox and mailbox():
                        # A message accepted while the model was streaming is
                        # still part of this assignment, not a completed reply.
                        continue
                    return finish_run(self, (final_answer), 'completed')
                    
            except Exception as e:
                if streaming_tool_entries:
                    await self._cancel_streaming_tool_tasks(streaming_tool_entries)
                from xuloom.agents.external.claude import ExternalExecutionError
                from xuloom.core.output_limits import ModelBudgetError
                if isinstance(e, (ExternalExecutionError, ModelBudgetError)):
                    return finish_run(self, str(e), e.reason if isinstance(e, ExternalExecutionError) else "budget_exhausted")
                if isinstance(e, ToolProtocolError):
                    # DSML is decoded atomically before dispatch. Earlier successful
                    # turns and their side effects must never be rolled back/replayed.
                    protocol_failures += 1
                    retrying = protocol_failures <= 2 and protocol_retries < 6
                    diagnostic = ""
                    try:
                        directory = artifact_directory(self.session_cwd) / self.session_id / "protocol-errors"
                        directory.mkdir(parents=True, exist_ok=True)
                        target = directory / (uuid.uuid4().hex + ".json")
                        target.write_text(json.dumps({"code": e.code, "detail": str(e), "tool": e.tool_name,
                            "turn": turn_count, "raw_completion": e.raw_text}, ensure_ascii=False, indent=2), encoding="utf-8")
                        diagnostic = str(target)
                    except OSError:
                        pass
                    if e.usage is not None:
                        self._record_token_usage(SimpleNamespace(usage=e.usage, content=[self._make_text_block(e.raw_text)]), loop_state, turn_count,
                            system_prompt=system_prompt, api_messages=api_messages, tools_schema=tools_schema,
                            thinking_config=effective_thinking_config)
                    message = (f"工具调用格式错误，正在请求模型修正（{protocol_failures}/2）。已完成的操作保留。" if retrying else
                               "工具调用格式多次修正失败，任务已中断。已完成的操作和文件保留，可继续当前会话。")
                    await self._record_loop_transition("tool_protocol_repair" if retrying else "tool_protocol_exhausted",
                        turn=turn_count, event_callback=event_callback, code=e.code, detail=str(e),
                        retrying=retrying, attempt=protocol_failures, diagnostic=diagnostic)
                    await self._emit_stream_event(event_callback, {"type": "tool_protocol_error", "message": message,
                        "retrying": retrying, "detail": str(e), "diagnostic": diagnostic})
                    sys_print_callback(message)
                    if e.visible_text.strip():
                        partial = create_assistant_message(e.visible_text, model=loop_state["active_model"], stop_reason="protocol_error")
                        partial["_is_meta"] = True
                        self.messages.append(partial)
                    self.messages.append(create_user_message(
                        "\n\n[Tool protocol error: current completion rejected before any of its calls executed]\n" + str(e) +
                        "\nCorrect the tool call using the currently declared tools schema. Prefer native function/tool_calls. "
                        "Arguments must be a JSON object; arrays must be actual arrays, not serialized strings. "
                        "If using DSML, use only declared parameter names, each exactly once, and close every tag. "
                        "Continue the user's selected skill/workflow from the last successful tool result. "
                        "Do not repeat completed commands, submissions or agent launches. Do not substitute an ordinary answer for the requested workflow.",
                        is_meta=True, origin="tool_protocol"))
                    self._sync_message_context()
                    self.persist_session_state()
                    if retrying:
                        protocol_retries += 1
                        continue
                    halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="tool_protocol_error")
                    halted["_is_meta"] = True
                    self.messages.append(halted)
                    self._sync_message_context()
                    self.persist_session_state()
                    return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + message), 'tool_protocol_error')
                if isinstance(e, AbortRequestedError):
                    rolled_back = self._rollback_incomplete_tool_turn(assistant_message)
                    await self._record_loop_transition(
                        "user_abort",
                        turn=turn_count,
                        event_callback=event_callback,
                        rolled_back=rolled_back,
                    )
                    self.active_stream = None
                    self.active_streaming_tool_entries = []
                    self.persist_session_state()
                    return finish_run(self, ("[bold yellow]Turn aborted.[/bold yellow] "
                        "The current agent turn was interrupted and cleanup has completed."), 'cancelled')
                if isinstance(e, StreamInterrupted):
                    partial = create_assistant_message(e.partial_text or str(e), model=loop_state["active_model"], stop_reason="stream_interrupted")
                    if not e.partial_text:
                        partial["_is_meta"] = True
                    self.messages.append(partial)
                    if e.tool_results:
                        self.messages.append(create_user_message("[Interrupted stream: already executed tool results; inspect before continuing, do not replay]\n" +
                            json.dumps(e.tool_results, ensure_ascii=False, default=str), is_meta=True, origin="stream_interrupted"))
                    self._sync_message_context()
                    self.persist_session_state()
                    return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + str(e)), 'stream_interrupted')
                if isinstance(e, PersistenceError):
                    return finish_run(self, "Storage checkpoint failed: " + str(e), 'persistence_error')
                from xuloom.core.model_inputs import UnsupportedInput
                if isinstance(e, UnsupportedInput):
                    return finish_run(self, str(e), 'unsupported_input')
                if (
                    self._is_model_fallback_error(e)
                    and loop_state["active_model"] != self.fallback_model
                    and self.fallback_model
                ):
                    self._strip_thinking_signatures_from_history()
                    loop_state["active_model"] = self.fallback_model
                    loop_state["fallback_count"] += 1
                    await self._record_loop_transition(
                        "model_fallback",
                        turn=turn_count,
                        event_callback=event_callback,
                        fallback_model=self.fallback_model,
                        error=str(e),
                    )
                    sys_print_callback(
                        f"[dim yellow]↳ Primary model failed; retrying current turn with fallback model {self.fallback_model}.[/dim yellow]"
                    )
                    continue
                if self._is_context_overflow_error(e):
                    retries = loop_state.get("reactive_compact_retries", 0)

                    # Withhold-then-recover: attempt transparent recovery
                    # without surfacing the error to the user. Each stage
                    # is progressively more aggressive; if any succeeds the
                    # loop continues seamlessly.

                    # Stage 1: reduce max_tokens (zero-cost, no data loss)
                    current_max = loop_state["max_tokens"]
                    if retries == 0 and current_max > 4096:
                        new_max = max(4096, current_max // 2)
                        loop_state["max_tokens"] = new_max
                        loop_state["reactive_compact_retries"] = 1
                        await self._record_loop_transition(
                            "reactive_withhold_stage1",
                            turn=turn_count,
                            event_callback=event_callback,
                            old_max=current_max,
                            new_max=new_max,
                        )
                        sys_print_callback(
                            f"[dim yellow]↳ Context overflow withheld (stage 1). "
                            f"Reducing max_tokens {current_max} → {new_max}.[/dim yellow]"
                        )
                        continue

                    # Stage 2: normal compaction + post-compact restore
                    if retries <= 1:
                        compacted = await self._apply_layered_compaction_if_needed(
                            sys_print_callback,
                            force=True,
                            aggressive=False,
                        )
                        if compacted:
                            loop_state["reactive_compact_retries"] = 2
                            loop_state["max_tokens"] = self._get_max_output_tokens()
                            await self._record_loop_transition(
                                "reactive_withhold_stage2",
                                turn=turn_count,
                                event_callback=event_callback,
                                retries=2,
                                stage="normal",
                                max_tokens_restored=loop_state["max_tokens"],
                            )
                            sys_print_callback(
                                "[dim magenta]↳ Context overflow withheld (stage 2). "
                                "Retrying after normal compaction + state restore.[/dim magenta]"
                            )
                            continue

                    # Stage 3: aggressive compaction + restore
                    if retries <= 2:
                        compacted = await self._apply_layered_compaction_if_needed(
                            sys_print_callback,
                            force=True,
                            aggressive=True,
                        )
                        if compacted:
                            loop_state["reactive_compact_retries"] = 3
                            loop_state["max_tokens"] = self._get_max_output_tokens()
                            await self._record_loop_transition(
                                "reactive_withhold_stage3",
                                turn=turn_count,
                                event_callback=event_callback,
                                retries=3,
                                stage="aggressive",
                                max_tokens_restored=loop_state["max_tokens"],
                            )
                            sys_print_callback(
                                "[dim magenta]↳ Context overflow withheld (stage 3). "
                                "Retrying after aggressive compaction + state restore.[/dim magenta]"
                            )
                            continue
                rolled_back = self._rollback_incomplete_tool_turn(assistant_message)
                if rolled_back:
                    return finish_run(self, ("[bold red]API Loop Exception:[/bold red] "
                        f"{str(e)}\n"
                        "[dim]Rolled back the incomplete assistant tool_use turn to keep session history resumable.[/dim]"), 'api_error')
                # Catch Anthropic Auth errors or general crashes
                return finish_run(self, (f"[bold red]API Loop Exception:[/bold red] {str(e)}"), 'api_error')
                
        return finish_run(self, ("[bold red]System Halt:[/bold red] "
            f"Max internal reasoning turns ({loop_state['max_turns']}) reached without returning an answer."), 'turn_limit')

    async def undo_file(self, revision_id):
        """An explicit user/API action; never a model tool or an automatic replay."""
        if self.has_active_turn() or getattr(self, "_goal_task", None) is not None:
            raise ValueError("Stop the active turn or goal before undoing a file")
        if any(not task.done() for task in self.tool_context["subagent_tasks"].tasks.values()) or any(
            row["status"] == "running" for row in self.tool_context["command_sessions"].records.values()):
            raise ValueError("Stop owned agents and commands before undoing a file")
        self.require_writer()
        if self.execution_sandbox.enabled:
            raise ValueError("当前系统隔离后端尚不支持文件撤销；未执行宿主写入。")
        self.active_run_task = asyncio.current_task()
        try:
            from xuloom.features.file_revisions import restore_revision
            result = await restore_revision(self.tool_context, revision_id)
            if not self.persist_session_state():
                raise PersistenceError("File was restored, but the session checkpoint failed. Do not replay other operations; undo itself is idempotent.")
            return result
        finally:
            self.active_run_task = None

    async def run(self, user_input: str, sys_print_callback=print, *, selected_skills=None):
        if self.has_active_turn() or getattr(self, "_goal_task", None) is not None and asyncio.current_task() not in (self._goal_task, getattr(self, "_goal_turn_task", None)):
            return RunResult("A turn is already running in this engine; wait or cancel it first.", "session_busy", session_id=self.session_id, agent_id=self.agent_id)
        self.active_run_task = asyncio.current_task()
        try:
            return await self._run_loop(user_input, sys_print_callback=sys_print_callback, selected_skills=selected_skills)
        except asyncio.CancelledError:
            finish_run(self, "Execution was cancelled; completed effects are retained.", "cancelled")
            raise
        finally:
            self.active_run_task = None

    async def submit_message(self, user_input: str, sys_print_callback=print, input_blocks=None, attachments=None, display_text=None, selected_skills=None, resume=False):
        if self.has_active_turn() or getattr(self, "_goal_task", None) is not None and asyncio.current_task() not in (self._goal_task, getattr(self, "_goal_turn_task", None)):
            result = RunResult("A turn is already running in this engine; wait or cancel it first.", "session_busy", session_id=self.session_id, agent_id=self.agent_id)
            yield {"type": "final", "text": result.text, "result": result.to_dict()}
            return
        queue = asyncio.Queue()

        async def emit(payload: dict):
            await queue.put(payload)

        task = asyncio.create_task(
            self._run_loop(
                user_input,
                sys_print_callback=sys_print_callback,
                event_callback=emit,
                input_blocks=input_blocks,
                attachments=attachments,
                display_text=display_text,
                selected_skills=selected_skills,
                resume=resume,
            )
        )
        self.active_run_task = task

        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=0.05)
                    yield payload
                except asyncio.TimeoutError:
                    continue

            result = await task
            yield {"type": "final", "text": result.text, "result": result.to_dict()}
        finally:
            if not task.done():
                self.abort_event.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.tool_context["subagent_tasks"].close()
            self.active_run_task = None
            self.active_stream = None
            self.active_streaming_tool_entries = []
