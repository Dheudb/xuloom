"""Session loading, metadata, persistence and parent/child state transfer."""
import copy
from xuloom.core.reasoning import configured_profile, normalize_profile
from xuloom.core.session_lock import SessionBusyError
from xuloom.core.storage import artifact_directory
from xuloom.core.tool_execution import restore_tool_activity, restore_tool_catalog
from xuloom.memory.memory import MemoryCompactor
from xuloom.utils.command_sessions import CommandSessions


class SessionPersistenceMixin:
    """Session loading, metadata, persistence and parent/child state transfer."""

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
            from xuloom.agents.external.runtime import restore as restore_external
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
            from xuloom.core.steering import restore as restore_input
            self.user_inbox = restore_input(metadata.get("user_inbox", []))
            if self.user_inbox: self.user_input_event.set()
            else: self.user_input_event.clear()
            self.last_run_result = copy.deepcopy(metadata.get("last_run_result"))
            self.native_context = copy.deepcopy(metadata.get("native_context"))
            self.last_compaction = copy.deepcopy(metadata.get("last_compaction"))
            self.context_calibration = copy.deepcopy(metadata.get("context_calibration", {}))
            self.auxiliary_usage = copy.deepcopy(metadata.get("auxiliary_usage", {}))
            restore_tool_activity(self, metadata)
            restore_tool_catalog(self, metadata)
            self.agent_id = metadata.get("agent", {}).get("agent_id") or self.agent_id
            self.tool_context["agent_id"] = self.agent_id
            self.tool_context["file_changes"] = metadata.get("file_changes", [])
            self.tool_context["workspace_moves"] = metadata.get("workspace_moves", [])
            self.turn_traces = metadata.get("turn_traces", [])
            self.model_selection = metadata.get("model_selection")
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
                    request_owner=self._request_owner,
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
            self.tool_context["pipelines"] = copy.deepcopy(metadata.get("pipelines", {}))
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
            from xuloom.core.continuation import restore
            restore(self, metadata.get("native_continuation"))
            return True
        return False

    def _build_session_metadata(self) -> dict:
        return {
            "native_continuation": copy.deepcopy(self.tool_context.get("native_continuation")),
            "external_agent": copy.deepcopy(self.tool_context.get("external_agent")),
            "execution_mode": self.execution_sandbox.mode,
            "collaboration_mode": self.collaboration_mode,
            "collaboration_run": copy.deepcopy(self.tool_context.get("collaboration_run")),
            "goal": self.goal,
            "scheduled_deliveries": self.scheduled_deliveries,
            "user_inbox": copy.deepcopy(self.user_inbox),
            "native_context": copy.deepcopy(self.native_context),
            "last_compaction": copy.deepcopy(self.last_compaction),
            "context_calibration": copy.deepcopy(self.context_calibration),
            "auxiliary_usage": copy.deepcopy(self.auxiliary_usage),
            "file_changes": self.tool_context.get("file_changes", []),
            "tool_activity": {"in_progress": self.in_progress_tools, "recent": self.recent_tool_activity},
            "activated_tools": sorted(self.activated_tools),
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
            "pipelines": copy.deepcopy(self.tool_context.get("pipelines", {})),
            "delegation": self.tool_context.get("delegation", {}),
            "turn_traces": self.turn_traces,
            "model_selection": getattr(self, "model_selection", None),
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
