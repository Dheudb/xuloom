"""QueryEngine: the shared execution loop over explicit model connections, context and tools.

The engine is composed from focused mixins in :mod:`xuloom.core.engine_parts`; this module keeps
construction, runtime connection switching, lifecycle (open/close/abort) and the public entry points.
"""
import asyncio
import copy
import os
import uuid
import inspect
import logging
from typing import Optional
from xuloom.agents.registry import AgentRegistry
from xuloom.agents.tasks import SubagentTasks
from xuloom.agents.team import TeamManager
from xuloom.branding import configuration_file
from xuloom.context.attachments import AttachmentCollector
from xuloom.context.context import ContextBuilder
from xuloom.context.file_state_cache import FileStateCache
from xuloom.context.memory_files import MemoryFileManager
from xuloom.core import tool_catalog
from xuloom.core.hooks import HookManager
from xuloom.core.model_stream import AbortRequestedError, ModelStream
from xuloom.core.reasoning import configured_profile, normalize_profile, request_options
from xuloom.core.run_result import PersistenceError, RunResult, finish_run
from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionConflictError
from xuloom.core.storage import artifact_directory, history_root
from xuloom.features.artifact_tracker import ArtifactTracker
from xuloom.features.auto_commit import AutoCommitManager
from xuloom.features.browser import BrowserSessionManager
from xuloom.features.content_replacement import ContentReplacementManager
from xuloom.features.incremental_write_queue import IncrementalWriteQueue
from xuloom.features.plans import PlanManager
from xuloom.features.structured_output import StructuredOutputManager
from xuloom.features.todos import TodoManager
from xuloom.features.ultraplan import UltraplanManager
from xuloom.features.verification import VerificationManager
from xuloom.features.workflow_completion import WorkflowCompletion
from xuloom.memory.extract_memories import MemoryExtractor
from xuloom.memory.memory import MemoryCompactor
from xuloom.memory.session_memory import SessionMemoryManager
from xuloom.protocols.lsp_manager import LSPManager
from xuloom.protocols.mcp_bridge import MCPBridge
from xuloom.security.permissions import PermissionManager
from xuloom.security.sandbox import SandboxManager
from xuloom.security.security_classifier import AutoSecurityClassifier
from xuloom.skills.registry import SkillRegistry
from xuloom.models.registry import client_adapter
from xuloom.core.engine_config import (
    EngineConfig,
    environment_api_key,
    environment_base_url,
    resolve_fallback_model,
    resolve_model_provider,
)
from xuloom.tools.builtin import LATENT_TOOL_NAMES, default_tools
from xuloom.tools.lsp_tool import LspTool
from xuloom.utils.shell_tasks import ShellTaskManager
from xuloom.utils.vcr import VCRManager
_engine_logger = logging.getLogger("xuloom.core.engine")
__all__ = ["QueryEngine", "EngineConfig", "AbortRequestedError", "RunResult", "PersistenceError"]

from xuloom.core.engine_parts import (
    MessageNormalizationMixin,
    SessionPersistenceMixin,
    ContextManagementMixin,
    ReasoningMixin,
    ModesMixin,
    AutoCommitMixin,
    StatusMixin,
    ToolOrchestrationMixin,
    TurnLoopMixin,
)


class QueryEngine(
    MessageNormalizationMixin,
    SessionPersistenceMixin,
    ContextManagementMixin,
    ReasoningMixin,
    ModesMixin,
    AutoCommitMixin,
    StatusMixin,
    ToolOrchestrationMixin,
    TurnLoopMixin,
    ModelStream,
):
    """
    The shared execution loop over explicit model connections, context and tools.
    """
    def __init__(self, config: Optional[EngineConfig] = None, /, *, permission_handler=None, **options):
        """Create an engine from an :class:`EngineConfig` and/or keyword options.

        ``QueryEngine(EngineConfig(model=...))`` and ``QueryEngine(model=...)`` are
        equivalent; keyword options override fields of ``config``. Environment
        defaults are applied once by :meth:`EngineConfig.resolved` and the result
        is available as ``engine.config``.
        """
        if config is not None and not isinstance(config, EngineConfig):
            raise TypeError("QueryEngine expects an EngineConfig; pass other settings as keyword options")
        config = (config or EngineConfig()).replace(**options)
        # Plugins load first: they may register the model adapter named by the config.
        self.plugins = self._load_plugins(config)
        try:
            config = config.resolved()
            self.config = config
            self._init_model_runtime(config)
            self.agent_id = config.agent_id or str(uuid.uuid4())
            self.parent_agent_id = config.parent_agent_id
            self.agent_depth = config.agent_depth
            self.agent_role = config.agent_role
            self._init_managers(config, permission_handler)
            self._init_tools(config)
            self._init_tool_context(permission_handler)
            for tool in config.extra_tools:
                self.register_tool(tool)
            self._apply_plugins()
        except BaseException:
            # A failed construction leaves no plugin adapters or open clients behind.
            for plugin in reversed(self.plugins):
                plugin.rollback_adapters()
            self._discard_model_clients()
            raise

    def _load_plugins(self, config: EngineConfig):
        """Load explicit plugins (errors raise) and, when asked, the user's enabled plugins (errors are recorded)."""
        from xuloom.plugins import enabled_plugin_names, load_plugins
        loaded = load_plugins(config.plugins, strict=True)
        if config.load_enabled_plugins:
            explicit = {item.name for item in loaded}
            try:
                names = [name for name in enabled_plugin_names() if name not in explicit]
            except Exception as error:
                from xuloom.plugins import LoadedPlugin
                return loaded + [LoadedPlugin("plugins.json", "", None, None, f"{type(error).__name__}: {error}")]
            loaded += load_plugins(names, strict=False)
        return loaded

    def _apply_plugins(self):
        """Install plugin tools, hooks and Skill/Subagent directories, one plugin at a time, all-or-nothing."""
        skills = self.tool_context["skill_registry"]
        agents = self.tool_context["agent_registry"].skills
        for plugin in self.plugins:
            if not plugin.ok:
                continue
            tools, hooks, roots = [], [], []
            try:
                for tool in plugin.api.tools:
                    tools.append(self.register_tool(tool))
                for event, callback, tool_filter, modes in plugin.api.hooks:
                    hooks.append(self.hook_manager.register(event, callback, tools=tool_filter, modes=modes))
                for registry in (skills, agents):
                    for kind, directories in (("skills", plugin.api.skill_dirs), ("agents", plugin.api.agent_dirs)):
                        for directory in directories:
                            registry.plugin_roots[kind].append(directory)
                            roots.append((registry.plugin_roots[kind], directory))
            except Exception as error:
                for name in tools:
                    self.available_tools.pop(name, None)
                    self.activated_tools.discard(name)
                for handle in hooks:
                    self.hook_manager.unregister(handle)
                for bucket, directory in roots:
                    bucket.remove(directory)
                plugin.rollback_adapters()
                if plugin.source == "object":
                    raise
                plugin.error = f"{type(error).__name__}: {error}"
                _engine_logger.warning("Plugin %s was not applied: %s", plugin.name, plugin.error)
                if any(client_adapter(client) in plugin.api.adapters for client in self._model_clients()):
                    # The model itself comes from the failed plugin: never run it without the
                    # rest of the plugin (e.g. its protective hooks).
                    from xuloom.plugins import PluginError
                    raise PluginError(f"Plugin {plugin.name!r} provides this engine's model adapter "
                                      f"but failed to load: {plugin.error}") from error
        # Workers inherit the same plugin set without re-reading the user's enabled list.
        self.tool_context["plugin_specs"] = tuple(plugin.spec for plugin in self.plugins if plugin.ok)

    def _model_clients(self):
        clients = [getattr(self, "client", None), getattr(getattr(self, "compactor", None), "client", None)]
        clients += list(getattr(self, "_retired_model_clients", []))
        return [client for client in clients if client is not None]

    def _discard_model_clients(self):
        """Close clients of an engine whose construction failed (no aclose() will follow)."""
        from xuloom.models.registry import unbind_client
        for client in self._model_clients():
            try:
                closing = client.close()
                if inspect.isawaitable(closing):
                    try:
                        asyncio.get_running_loop().create_task(closing)
                    except RuntimeError:
                        asyncio.run(closing)
            except Exception:
                pass
            unbind_client(client)

    @property
    def plugin_errors(self):
        return {plugin.name: plugin.error for plugin in self.plugins if not plugin.ok}

    def _init_model_runtime(self, config: EngineConfig):
        """Bind the model connection, reasoning profile and thinking defaults."""
        from xuloom.security.execution import ExecutionSandbox
        from xuloom.core.network import model_network_mode
        self.session_cwd = config.cwd
        self.execution_sandbox = ExecutionSandbox(self.session_cwd, config.execution_mode)
        self.collaboration_mode = config.collaboration_mode
        self.reasoning_effort = config.reasoning_effort
        self.model_provider = config.model_provider
        self.api_base_url = config.api_base_url
        self._api_key = config.api_key
        self.network_mode = model_network_mode(config.network_mode)
        self._retired_model_clients = []
        self.local_tokenizer_path = config.local_tokenizer_path
        self.primary_model = config.model
        # A resolved role binding supplies an exact account contract, including
        # explicit unknown capabilities. Do not guess another same-URL account.
        model_contract = config.model_contract
        self._model_contract = copy.deepcopy(dict(model_contract)) if model_contract is not None else None
        profile = model_contract.get("reasoning") if model_contract is not None else configured_profile(self.model_provider, self.primary_model, self.api_base_url)
        self.reasoning_profile = normalize_profile(profile, self.model_provider) if profile is not None else None
        if self.reasoning_effort and self.model_provider != "openai" and self.reasoning_profile is None:
            raise ValueError("Anthropic effort requires an explicit model reasoning profile")
        if self.reasoning_profile is not None:
            request_options(self.reasoning_profile, self.model_provider, effort_override=self.reasoning_effort)
        self.fallback_model = config.fallback_model
        self.model = self.primary_model
        self.client = self._build_model_client()
        thinking_config, mode_thinking_overrides = config.thinking_config, config.mode_thinking_overrides
        self._thinking_config_explicit = thinking_config is not None or bool(mode_thinking_overrides)
        self.default_thinking_config = self._normalize_thinking_config(
            thinking_config if thinking_config is not None else {"type": "adaptive", "display": "summarized"}
        )
        normalized_mode_overrides = {
            "plan": {"type": "disabled"},
        }
        for mode_name, mode_config in dict(mode_thinking_overrides or {}).items():
            normalized = self._normalize_thinking_config(mode_config)
            if normalized is None:
                normalized_mode_overrides.pop(str(mode_name), None)
            else:
                normalized_mode_overrides[str(mode_name)] = normalized
        self.mode_thinking_overrides = normalized_mode_overrides

    def _init_managers(self, config: EngineConfig, permission_handler):
        """Create per-session managers (context, hooks, memory, permissions, storage, ...)."""
        approval_policy = config.approval_policy
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
            request_owner=self._request_owner,
        )
        self.compactor.usage_callback = self._record_auxiliary_usage
        self.native_context = None
        self.last_compaction = None
        self.context_calibration = {}
        self.auxiliary_usage = {}
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
        self.user_inbox = []
        self.user_input_event = asyncio.Event()
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

    def _init_tools(self, config: EngineConfig):
        """Install the built-in tool set; latent tools stay hidden until activated."""
        all_tools = default_tools()
        for name in config.disabled_tools:
            all_tools.pop(name, None)
        self.builtin_tool_names = frozenset(all_tools)
        self.available_tools = {
            name: tool for name, tool in all_tools.items() if name not in LATENT_TOOL_NAMES
        }
        self.latent_tools = {
            name: tool for name, tool in all_tools.items() if name in LATENT_TOOL_NAMES
        }
        self.activated_tools = set()

    def _init_tool_context(self, permission_handler):
        """Build the shared context handed to every tool and wire runtime callbacks."""
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
            "incremental_write_queue": self.incremental_write_queue,
            "auto_commit_manager": self.auto_commit_manager,
            "content_replacement_manager": self.content_replacement_manager,
            "sandbox_manager": self.sandbox_manager,
            "engine_available_tools": self.available_tools,
            "latent_tools_registry": self.latent_tools,
            "model_tool_names": lambda: set(tool_catalog.visible(self)),
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
            "pipelines": {},
            "coordinate_agents": self.agents,
            "persist_pipeline": self.persist_session_state,
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
        self.execution_sandbox.notify = lambda event: self.tool_context["capability_event"](event) if callable(self.tool_context.get("capability_event")) else None
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
        next_base = api_base_url if api_base_url is not None else (self.api_base_url if next_provider == self.model_provider else environment_base_url(next_provider))
        next_key = api_key if api_key is not None else (self._api_key if next_provider == self.model_provider else environment_api_key(next_provider))
        route_changed = (next_provider, next_model, next_base, next_key) != (self.model_provider, self.primary_model, self.api_base_url, self._api_key)
        next_fallback = self._resolve_fallback_model(None, provider=next_provider, primary_model=next_model)
        next_profile = configured_profile(next_provider, next_model, next_base)
        next_client = create_model_client(provider=next_provider, model=next_model, api_base_url=next_base, api_key=next_key, network_mode=next_network, request_owner=self._request_owner)
        try:
            next_compactor = MemoryCompactor(model=next_fallback or next_model, provider=next_provider,
                api_base_url=next_base, local_tokenizer_path=self.local_tokenizer_path, api_key=next_key, network_mode=next_network, request_owner=self._request_owner)
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
        self.compactor.usage_callback = self._record_auxiliary_usage
        self.context_calibration = {}
        from xuloom.context.compaction import configured as context_settings
        if self.native_context and (route_changed or context_settings(self)["backend"] != "openai-responses"):
            # The canonical opaque state is route-specific; raw local messages stay portable.
            self.messages = [message for message in self.messages if message.get("_origin") != "compaction"]
            for message in self.messages: message.pop("_responses_output", None)
            self.native_context = None
            self._update_compact_boundary()
        if route_changed:
            for message in self.messages: message.pop("_responses_output", None)
        self.config = self.config.replace(
            model_provider=next_provider, model=next_model, api_base_url=next_base, api_key=next_key,
            network_mode=next_network, fallback_model=next_fallback, model_contract=None, reasoning_effort=None)
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
        self.compactor.usage_callback = self._record_auxiliary_usage
        self.compactor.window_limit = lambda: __import__("xuloom.context.compaction", fromlist=["policy"]).policy(self, self.compactor.model)["window"]
        self.tool_context["context_profile"] = lambda: __import__("xuloom.context.compaction", fromlist=["configured"]).configured(self)
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
        from xuloom.tools.native_search import configured, normalize
        search = (self._model_contract or {}).get('search') if self._model_contract is not None else configured(
            self.model_provider, self.primary_model, self.api_base_url, self._api_key)
        self.tool_context['native_search_options'] = lambda: {
            'profile': normalize(search), 'model': self.primary_model, 'base_url': self.api_base_url or str(getattr(self.client, 'base_url', '')),
            'api_key': self._api_key, 'network_mode': self.network_mode, 'owner': self._request_owner}

    def _resolve_model_provider(self, provider: str = None, model: str = None) -> str:
        return resolve_model_provider(provider, model)

    def _resolve_fallback_model(self, fallback_model: Optional[str], *, provider=None, primary_model=None) -> str:
        provider = provider if provider is not None else self.model_provider
        primary_model = primary_model if primary_model is not None else self.primary_model
        return resolve_fallback_model(provider, fallback_model, primary_model)

    def _build_model_client(self):
        from xuloom.core.network import create_model_client
        return create_model_client(provider=self.model_provider, model=self.primary_model,
            api_base_url=self.api_base_url, api_key=self._api_key, network_mode=self.network_mode, request_owner=self._request_owner)

    def _model_adapter(self):
        """The adapter that created this engine's client (fixed for the client's lifetime)."""
        from xuloom.models.registry import client_adapter, get_adapter
        return client_adapter(self.client) or get_adapter(self.model_provider)

    def _request_owner(self):
        return getattr(self, "tool_context", {}).get("delegation", {}).get("owner_session_id") or self.session_id

    def _input_contract(self, model):
        from xuloom.core.model_inputs import configured_inputs
        if self._model_contract is not None and model == self.primary_model:
            return self._model_contract.get("inputs")
        return configured_inputs(self.model_provider, model, self.api_base_url)

    @property
    def is_configured(self) -> bool:
        """Check if minimum configuration (provider + model) is present."""
        return bool(self.model_provider and self.primary_model)

    def has_active_turn(self) -> bool:
        return bool(getattr(self, "_context_compacting", False)) or self.active_run_task is not None and not self.active_run_task.done()

    def queue_input(self, text: str, request_id=None):
        from xuloom.core.steering import enqueue
        return enqueue(self, text, request_id)

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

        if self.active_run_task is None or self.active_run_task.done():
            return True

        try:
            await asyncio.wait_for(asyncio.shield(self.active_run_task), timeout=1.5)
        except asyncio.TimeoutError:
            self.active_run_task.cancel()
            await asyncio.gather(self.active_run_task, return_exceptions=True)
        except Exception:
            pass

        return True
            
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
        for issue in self.mcp_bridge.configuration_issues:
            sys_print("MCP configuration: " + issue["path"] + ": " + issue["error"])
        for name, status in self.mcp_bridge.statuses.items():
            if status.get("status") == "error":
                sys_print("MCP " + name + ": " + status["error"])
        if remote_tools:
            self.register_mcp_tools(remote_tools)
            sys_print(f"[dim green]↳ Protocol Matrix Online: Discovered {len(remote_tools)} MCP tools; load details with tool_search_tool.[/dim green]")

    def _assert_open(self):
        if getattr(self, "_close_task", None) is not None:
            raise SessionConflictError("Engine is closing or closed; create a new engine before executing.")

    async def aclose(self):
        task = getattr(self, "_close_task", None)
        if task is None or task.done() and (task.cancelled() or task.exception() is not None):
            task = self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(task)

    async def _close(self):
        if self.hook_manager.has_callbacks("SessionEnd") or self.hook_manager.has_configured("SessionEnd"):
            await self._dispatch_hook("SessionEnd", self._hook_payload("SessionEnd"), queue_context=False)
        # Observe-only hooks fired from synchronous code finish before resources go away.
        pending_hooks = [task for task in self.__dict__.get("_hook_tasks", ()) if not task.done()]
        if pending_hooks:
            await asyncio.gather(*pending_hooks, return_exceptions=True)
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
        cleanup = [self.client.close()] if self.client is not None else []
        if getattr(self.compactor, "client", None):
            cleanup.append(self.compactor.client.close())
        cleanup.extend(client.close() for client in self._retired_model_clients if not client.is_closed)
        if self.agent_depth == 0:
            cleanup.append(self.browser_manager.close())
            cleanup.extend(server.stop() for server in getattr(getattr(self, "lsp_manager", None), "servers", {}).values())
            if getattr(self, "mcp_bridge", None):
                cleanup.append(self.mcp_bridge.cleanup())
        failures.extend(await asyncio.gather(*cleanup, return_exceptions=True))
        from xuloom.models.registry import unbind_client
        for client in self._model_clients():
            unbind_client(client)
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

    async def run(self, user_input: str, sys_print_callback=print, *, selected_skills=None):
        if self.has_active_turn() or getattr(self, "_goal_task", None) is not None and asyncio.current_task() not in (self._goal_task, getattr(self, "_goal_turn_task", None)):
            return RunResult("A turn is already running in this engine; wait or cancel it first.", "session_busy", session_id=self.session_id, agent_id=self.agent_id)
        if self.user_inbox:
            return RunResult('Resume the stopped task or withdraw its queued instructions before starting another task.', 'input_required')
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
        if not resume and self.user_inbox:
            result = RunResult('Resume the stopped task or withdraw its queued instructions before starting another task.', 'input_required')
            yield {'type':'final','text':result.text,'result':result.to_dict()}
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
            if task.cancelled():
                finish_run(self, "Event consumer cancelled execution; completed effects and uncertain tool receipts are retained.", "cancelled")
            self.active_run_task = None
            self.active_stream = None
            self.active_streaming_tool_entries = []
