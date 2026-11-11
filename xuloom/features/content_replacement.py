import logging
import copy
import os
import time
import uuid as _uuid
from typing import Optional
from xuloom.core.storage import artifact_directory


TOOL_RESULT_BUDGET_CHARS = 200_000
PREVIEW_CHARS = 2000
MIN_REPLACE_CHARS = 500


class ContentReplacementManager:
    def __init__(self, base_dir: str = None, session_id: str = None):
        self.base_dir = base_dir if base_dir is not None else str(artifact_directory())
        self.session_id = session_id
        self.registry = {}
        self.cleanup_history = []
        self.seen_ids: set[str] = set()
        self.replacements: dict[str, str] = {}

    def set_session(self, session_id: str):
        self.session_id = session_id

    def get_session_dir(self) -> str:
        if self.session_id:
            return os.path.abspath(os.path.join(self.base_dir, self.session_id))
        return os.path.abspath(self.base_dir)

    def register_spill(
        self,
        *,
        file_path: str,
        tool_name: str = "",
        tool_use_id: str = "",
        original_content_chars: int = 0,
        metadata: Optional[dict] = None,
    ) -> dict:
        abs_path = os.path.abspath(file_path)
        entry = {
            "path": abs_path,
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "original_content_chars": int(original_content_chars or 0),
            "metadata": copy.deepcopy(metadata or {}),
            "timestamp": time.time(),
            "exists": os.path.exists(abs_path),
        }
        self.registry[abs_path] = entry
        return entry

    def cleanup_orphans(self) -> list[dict]:
        session_dir = self.get_session_dir()
        if not os.path.isdir(session_dir):
            return []

        known_paths = set(self.registry.keys())
        removed = []
        for file_name in os.listdir(session_dir):
            file_path = os.path.abspath(os.path.join(session_dir, file_name))
            if not os.path.isfile(file_path):
                continue
            if file_path in known_paths:
                continue
            try:
                size = os.path.getsize(file_path)
            except Exception:
                size = 0
            try:
                os.remove(file_path)
                removed.append({
                    "path": file_path,
                    "size": size,
                    "timestamp": time.time(),
                })
            except Exception:
                continue

        if removed:
            self.cleanup_history = (removed + self.cleanup_history)[:20]
        return removed

    def mark_existing_entries(self):
        for path, entry in list(self.registry.items()):
            entry["exists"] = os.path.exists(path)

    def export_state(self) -> dict:
        self.mark_existing_entries()
        return {
            "session_id": self.session_id,
            "registry": copy.deepcopy(self.registry),
            "cleanup_history": copy.deepcopy(self.cleanup_history),
            "seen_ids": list(self.seen_ids),
            "replacements": copy.deepcopy(self.replacements),
        }

    def load_state(self, payload):
        payload = payload or {}
        self.session_id = payload.get("session_id", self.session_id)
        self.registry = dict(payload.get("registry", {}) or {})
        self.cleanup_history = list(payload.get("cleanup_history", []) or [])[:20]
        self.seen_ids = set(payload.get("seen_ids", []) or [])
        self.replacements = dict(payload.get("replacements", {}) or {})
        self.mark_existing_entries()

    def enforce_budget(self, messages: list, budget: int = 0) -> int:
        """Walk *messages* in-place and replace oversized tool_result blocks.

        Per-message semantics (matches Claude Code): each ``role: user``
        message is evaluated independently.  If the aggregate char size of
        its tool_result blocks exceeds *budget*, the largest **fresh**
        (never-before-seen) results are spilled to disk and replaced with
        a short preview.  Already-seen results keep their prior decision
        (frozen or already-replaced) so the wire payload is stable across
        turns.

        Returns the number of blocks newly replaced this call.
        """
        budget = budget or TOOL_RESULT_BUDGET_CHARS
        newly_replaced = 0

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            candidates: list[dict] = []
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                tid = block.get("tool_use_id", "")
                text = block.get("content", "")
                if not isinstance(text, str):
                    continue
                candidates.append({"block": block, "tid": tid, "size": len(text)})

            if not candidates:
                continue

            reapply: list[dict] = []
            frozen: list[dict] = []
            fresh: list[dict] = []
            for c in candidates:
                tid = c["tid"]
                if tid in self.replacements:
                    reapply.append(c)
                elif tid in self.seen_ids:
                    frozen.append(c)
                else:
                    fresh.append(c)

            for c in reapply:
                c["block"]["content"] = self.replacements[c["tid"]]

            if not fresh:
                for c in candidates:
                    self.seen_ids.add(c["tid"])
                continue

            frozen_size = sum(c["size"] for c in frozen)
            fresh_size = sum(c["size"] for c in fresh)

            if frozen_size + fresh_size <= budget:
                for c in candidates:
                    self.seen_ids.add(c["tid"])
                continue

            eligible = [c for c in fresh if c["size"] >= MIN_REPLACE_CHARS]
            eligible.sort(key=lambda c: c["size"], reverse=True)
            remaining = frozen_size + fresh_size
            for c in eligible:
                if remaining <= budget:
                    break
                replacement = self._spill_and_preview(c["block"], c["tid"], c["size"])
                if replacement is not None and len(replacement) < c["size"]:
                    saved = c["size"] - len(replacement)
                    c["block"]["content"] = replacement
                    self.replacements[c["tid"]] = replacement
                    remaining -= saved
                    newly_replaced += 1

            for c in candidates:
                self.seen_ids.add(c["tid"])

        return newly_replaced

    def _spill_and_preview(self, block: dict, tool_use_id: str, size: int) -> Optional[str]:
        """Write original content to disk and return a preview string."""
        original = block.get("content", "")
        if not isinstance(original, str) or not original:
            return None
        session_dir = self.get_session_dir()
        try:
            os.makedirs(session_dir, exist_ok=True)
            file_name = f"tool-result-{int(time.time())}-{_uuid.uuid4().hex[:8]}.txt"
            file_path = os.path.join(session_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(original)
            self.register_spill(
                file_path=file_path,
                tool_use_id=tool_use_id,
                original_content_chars=size,
            )
        except Exception as error:
            # Without a saved copy the preview would point at a missing file and lose the
            # output; keep the original content inline instead.
            logging.getLogger("xuloom.context").warning(
                "Could not save oversized tool result %s to disk (%s: %s); keeping it inline",
                tool_use_id, type(error).__name__, error)
            return None

        preview = original[:PREVIEW_CHARS]
        return (
            f"<persisted-output>\n"
            f"Output too large ({size:,} chars). Full output saved to: {file_path}\n\n"
            f"Preview (first {PREVIEW_CHARS} chars):\n"
            f"{preview}"
            + ("\n...\n" if len(original) > PREVIEW_CHARS else "\n")
            + "</persisted-output>"
        )

    def render_summary(self) -> str:
        self.mark_existing_entries()
        active_entries = [item for item in self.registry.values() if item.get("exists")]
        missing_entries = [item for item in self.registry.values() if not item.get("exists")]
        return (
            f"Content replacements: active={len(active_entries)}, "
            f"missing={len(missing_entries)}, "
            f"budget_replaced={len(self.replacements)}, "
            f"cleanup_events={len(self.cleanup_history)}"
        )
