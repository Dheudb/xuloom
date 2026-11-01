"""
Session Memory: maintains a rolling markdown summary of the current session.

Periodically extracts key information from the conversation using a lightweight
LLM call and writes it to a session-specific summary file. This summary is
then used during context compaction to preserve important session state.
"""

import asyncio
import copy
import hashlib
import json
import os
from xuloom.core.storage import atomic_text, data_directory, migrate_user_file
import time
from pathlib import Path
from typing import Optional

DEFAULT_SESSION_MEMORY_TEMPLATE = """\
# Session Title
_A short and distinctive 5-10 word descriptive title for the session._

# Current State
_What is actively being worked on right now? Pending tasks not yet completed._

# Task Specification
_User goals, constraints, requested deliverables and acceptance criteria._

# Sources and Artifacts
_Relevant evidence, references, data and outputs; distinguish observed facts from inference._

# Workflow
_Completed and pending steps, tool and worker identities, saved results and uncertain effects._

# Errors & Corrections
_Errors encountered and how they were fixed. Failed approaches to avoid._

# Learnings
_What has worked well? What has not? What to avoid?_

# Key Results
_Specific outputs requested by the user: answers, tables, documents._

# Worklog
_Step-by-step record of what was attempted and done. Terse summary per step._
"""

SESSION_MEMORY_UPDATE_PROMPT = """\
Based on the conversation history above, update the session notes file.

Here are its current contents:
<current_notes>
{current_notes}
</current_notes>

Produce the COMPLETE updated notes file. Maintain the exact section structure \
(all headers starting with # and italic descriptions starting/ending with _). \
Only update the content below each section's italic description.

Rules:
- Preserve goals, constraints, decisions, evidence references, artifacts and actual tool/worker states
- Distinguish user statements, verified observations, inference and unverified assistant claims
- Preserve uncertain effects and unfinished operations; a summary never grants new authorization or permits replay
- Keep each section under ~2000 tokens; condense by cycling out less important details
- Always update "Current State" to reflect the most recent work
- Skip sections with no new insights (leave blank, no filler)
- Do not reference these instructions in the notes
- Focus on actionable information for session continuity

Output ONLY the updated markdown file content, nothing else.
"""

MAX_SECTION_CHARS = 8000
MAX_TOTAL_CHARS = 48000


class SessionMemoryConfig:
    def __init__(
        self,
        minimum_tokens_to_init: int = 10000,
        minimum_tokens_between_update: int = 5000,
        tool_calls_between_updates: int = 3,
    ):
        self.minimum_tokens_to_init = minimum_tokens_to_init
        self.minimum_tokens_between_update = minimum_tokens_between_update
        self.tool_calls_between_updates = tool_calls_between_updates


class SessionMemoryManager:
    """
    Manages a rolling session memory file for the current conversation.

    The memory is stored at:
      ~/.xuloom/sessions/<session_id>/session-memory/summary.md

    It is updated periodically when token/tool-call thresholds are met,
    using a lightweight LLM summarisation call.
    """

    def __init__(
        self,
        session_id: str,
        compactor=None,
        config: SessionMemoryConfig = None,
        on_update=None,
    ):
        self.session_id = session_id
        self.compactor = compactor
        self.on_update = on_update
        self.config = config or SessionMemoryConfig()
        self._initialized = False
        self._tokens_at_last_extraction = 0
        self._tool_calls_since_update = 0
        self._last_summarised_msg_uuid: Optional[str] = None
        self._task = None
        self._closed = False
        self._generation = 0
        self.last_error = None
        self._memory_path: Optional[str] = None

    @property
    def memory_path(self) -> str:
        if self._memory_path is None:
            home = str(Path.home())
            session_dir = str(data_directory() / "memory/sessions" / self.session_id)
            os.makedirs(session_dir, exist_ok=True)
            self._memory_path = str(migrate_user_file(Path(home) / ".xuloom/sessions" / self.session_id / "session-memory/summary.md", Path(session_dir) / "summary.md"))
        return self._memory_path

    def record_tool_call(self):
        self._tool_calls_since_update += 1

    def should_extract(self, estimated_tokens: int) -> bool:
        if not self._initialized:
            if estimated_tokens < self.config.minimum_tokens_to_init:
                return False
            self._initialized = True

        token_growth = estimated_tokens - self._tokens_at_last_extraction
        has_met_token_threshold = token_growth >= self.config.minimum_tokens_between_update
        has_met_tool_threshold = self._tool_calls_since_update >= self.config.tool_calls_between_updates

        return has_met_token_threshold and has_met_tool_threshold

    async def maybe_extract(
        self,
        messages: list,
        estimated_tokens: int,
    ):
        """
        Check thresholds and, if met, run a background extraction.
        The engine owns cancellation and closure of this task.
        """
        if self._closed or not self.should_extract(estimated_tokens):
            return

        if self._task is not None and not self._task.done():
            return

        self._task = asyncio.create_task(self._run_extraction(copy.deepcopy(messages), estimated_tokens,
            self._tool_calls_since_update, self._generation))

    def cancel_pending(self):
        self._generation += 1
        if self._task is not None and not self._task.done():
            self._task.cancel()
            return self._task

    async def aclose(self):
        self._closed = True
        task = self.cancel_pending()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run_extraction(self, messages: list, estimated_tokens: int, tool_calls: int, generation: int):
        try:
            current_notes = self._read_current_notes()
            conversation_context = self._build_conversation_context(messages)
            updated_notes = await self._call_llm_for_update(conversation_context, current_notes)
            if self._closed or generation != self._generation:
                return
            if not updated_notes or not updated_notes.strip():
                self.last_error = "request_failed"
                return
            self._write_notes(self._truncate_if_needed(updated_notes))
            previous = self._tokens_at_last_extraction, self._tool_calls_since_update, self._last_summarised_msg_uuid
            self._tokens_at_last_extraction = estimated_tokens
            self._tool_calls_since_update = max(0, self._tool_calls_since_update - tool_calls)
            if messages and isinstance(messages[-1], dict) and messages[-1].get("_uuid"):
                self._last_summarised_msg_uuid = messages[-1]["_uuid"]
            self.last_error = None
            if self.on_update is not None:
                try:
                    saved = self.on_update()
                except Exception:
                    saved = False
                if saved is False:
                    self._tokens_at_last_extraction, self._tool_calls_since_update, self._last_summarised_msg_uuid = previous
                    self.last_error = "checkpoint_failed"
        except Exception:
            self.last_error = "extraction_failed"

    def _read_current_notes(self) -> str:
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return DEFAULT_SESSION_MEMORY_TEMPLATE

    def _write_notes(self, content: str):
        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        atomic_text(self.memory_path, content)

    def get_memory_content(self) -> Optional[str]:
        """Read the current session memory for use in compaction."""
        if not os.path.exists(self.memory_path):
            return None
        try:
            with open(self.memory_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content == DEFAULT_SESSION_MEMORY_TEMPLATE.strip():
                return None
            return content
        except Exception:
            return None

    def _build_conversation_context(self, messages: list, max_chars: int = 40000) -> str:
        lines = []
        total = 0
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if isinstance(content, str):
                line = f"[{role}]: {content}"
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            parts.append(block.get("text", "")[:2000])
                        elif btype == "tool_use":
                            parts.append(f"[tool_use:{block.get('name', '?')}]")
                        elif btype == "tool_result":
                            parts.append(f"[tool_result: {str(block.get('content', ''))[:300]}]")
                        elif btype in ("image", "document"):
                            parts.append(f"[{btype}]")
                line = f"[{role}]: {' '.join(parts)}"
            else:
                line = f"[{role}]: {str(content)[:2000]}"

            if total + len(line) > max_chars:
                break
            lines.insert(0, line)
            total += len(line)

        return "\n".join(lines)

    async def _call_llm_for_update(self, conversation: str, current_notes: str) -> Optional[str]:
        if self.compactor is None or self.compactor.client is None:
            return None

        prompt = SESSION_MEMORY_UPDATE_PROMPT.format(current_notes=current_notes)
        full_prompt = f"<conversation>\n{conversation}\n</conversation>\n\n{prompt}"

        try:
            from xuloom.core.config import get_model_max_output_tokens, safe_llm_call
            default_tokens, _ = get_model_max_output_tokens(self.compactor.model)

            result = await safe_llm_call(
                self.compactor.client, self.compactor.model,
                self.compactor.provider,
                [{"role": "user", "content": full_prompt}],
                default_tokens,
            )
            return result or None
        except Exception:
            return None

    def _truncate_if_needed(self, content: str) -> str:
        if len(content) <= MAX_TOTAL_CHARS:
            return content

        lines = content.split("\n")
        output_lines = []
        section_lines = []
        section_header = ""

        def flush_section():
            nonlocal section_lines, section_header
            if not section_header:
                output_lines.extend(section_lines)
            else:
                section_content = "\n".join(section_lines)
                if len(section_content) > MAX_SECTION_CHARS:
                    truncated_lines = []
                    char_count = 0
                    for sl in section_lines:
                        if char_count + len(sl) + 1 > MAX_SECTION_CHARS:
                            break
                        truncated_lines.append(sl)
                        char_count += len(sl) + 1
                    truncated_lines.append("\n[... section truncated for length ...]")
                    output_lines.append(section_header)
                    output_lines.extend(truncated_lines)
                else:
                    output_lines.append(section_header)
                    output_lines.extend(section_lines)
            section_lines = []
            section_header = ""

        for line in lines:
            if line.startswith("# "):
                flush_section()
                section_header = line
            else:
                section_lines.append(line)

        flush_section()
        return "\n".join(output_lines)

    def export_state(self) -> dict:
        return {
            "session_id": self.session_id,
            "initialized": self._initialized,
            "tokens_at_last_extraction": self._tokens_at_last_extraction,
            "tool_calls_since_update": self._tool_calls_since_update,
            "last_summarised_msg_uuid": self._last_summarised_msg_uuid,
            "memory_path": self.memory_path,
            "last_error": self.last_error,
        }

    def bind_session(self, session_id, state):
        self.session_id = session_id
        self._memory_path = None
        self.load_state(state)

    def load_state(self, state: dict):
        self.cancel_pending()
        state = state or {}
        self._initialized = state.get("initialized", False)
        self._tokens_at_last_extraction = state.get("tokens_at_last_extraction", 0)
        self._tool_calls_since_update = state.get("tool_calls_since_update", 0)
        self._last_summarised_msg_uuid = state.get("last_summarised_msg_uuid")
        self.last_error = state.get("last_error") if state.get("last_error") in {"request_failed", "extraction_failed", "checkpoint_failed"} else None
