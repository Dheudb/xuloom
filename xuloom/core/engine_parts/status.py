"""Human-readable runtime summaries for CLI and desktop."""
from xuloom.core.engine_state import EngineState


class StatusMixin(EngineState):
    """Human-readable runtime summaries for CLI and desktop."""

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
        output_remaining = self._remaining_output_budget(self._build_loop_state())
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
            f"Session cost: ${float(self.session_cost.get('usd', 0.0) or 0.0):.4f}"
            + (f" (+{self.session_cost.get('unpriced_requests')} unpriced requests)" if self.session_cost.get('unpriced_requests') else ""),
            f"Task cost limit: {'none' if self.cost_meter.limit_usd is None else f'${self.cost_meter.limit_usd:.4f}'}",
            f"Remaining output budget: {'unlimited' if output_remaining is None else output_remaining}",
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
