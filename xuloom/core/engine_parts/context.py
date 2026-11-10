"""Compaction boundaries, prompt caching, attachments and memory files."""
import json
import logging
import os
from xuloom.core.messages import create_user_message

_logger = logging.getLogger("xuloom.core.engine")


class ContextManagementMixin:
    """Compaction boundaries, prompt caching, attachments and memory files."""


    # ── Compact Boundary Slicing ──────────────────────────────────────
    def _find_last_compact_boundary_index(self) -> int:
        """Find the index of the last compaction boundary message."""
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            content = msg.get("content", "")
            if msg.get("_is_meta") and msg.get("_origin") != "human" and isinstance(content, str) and content.startswith("[CompactionBoundary:"):
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

    def refresh_memory_files(self):
        self.memory_file_manager.set_cwd(self.session_cwd)
        loaded = self.memory_file_manager.refresh()
        self.tool_context["memory_files"] = self.memory_file_manager.export_state()
        return loaded

    def _record_auxiliary_usage(self, purpose, usage):
        counts = self.auxiliary_usage.setdefault(purpose, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        counts["calls"] += 1
        for name in ("input_tokens", "output_tokens"):
            number = max(0, int(usage.get(name, 0) or 0))
            counts[name] += number
            self.session_token_usage[name] = self.session_token_usage.get(name, 0) + number

    def get_memory_summary(self) -> str:
        return self.memory_file_manager.render_prompt_summary()

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

    async def _apply_layered_compaction_if_needed(self, sys_print_callback=print, *, force=False, aggressive=False, focus="", event_callback=None):
        from xuloom.context.compaction import compact
        return await compact(self, sys_print_callback, force=force, aggressive=aggressive, focus=focus, event_callback=event_callback)

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
