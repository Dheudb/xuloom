"""Provider-neutral message shaping and API history cleanup."""
from typing import Any, Dict, List
import copy
import json
from types import SimpleNamespace
from xuloom.core.engine_state import EngineState
from xuloom.core.reasoning import transport_reasoning


class MessageNormalizationMixin(EngineState):
    """Provider-neutral message shaping and API history cleanup."""



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
        rendered: List[Dict[str, Any]] = []
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
                        **({"reasoning_content": msg["_openai_reasoning"]["content"]} if "_openai_reasoning" in msg else {}),
                    })
                continue

            user_parts = []
            tool_images: List[Any] = []
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

    def _get_anthropic_tools_schema(self):
        """Converts registered Pydantic tool classes into provider-native schemas."""
        from xuloom.core.tool_catalog import visible, compact_definition
        effective_tools = visible(self)
        schemas = []
        for tool in effective_tools.values():
            from xuloom.core.continuation import schema
            definition = schema(compact_definition(tool.get_tool_definition()))
            if tool.name == 'web_search_tool' and self.tool_context['native_search_options']()['profile']['backend'] == 'deepseek':
                definition['input_schema']['properties']['query']['maxLength'] = 128
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
        merged: List[Any] = []
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
            normalized: List[Dict[str, Any]] = [{"type": "text", "text": content}]
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
                **({"_openai_reasoning": state} if (state := transport_reasoning(self, msg.get("_openai_reasoning"))) is not None else {}),
            })

        normalized_messages = self._filter_incomplete_tool_messages_for_api(normalized_messages)
        normalized_messages = self._strip_error_causing_attachments(normalized_messages)

        collapsed: List[Any] = []
        for msg in normalized_messages:
            if not collapsed:
                collapsed.append(msg)
                continue
            if (collapsed[-1]["role"] == msg["role"]
                    and not (msg["role"] == "assistant" and ("_openai_reasoning" in msg or "_openai_reasoning" in collapsed[-1]))):
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
