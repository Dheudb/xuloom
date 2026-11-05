"""
Verification Agent

Optional assessment support. The generic engine does not automatically invoke it.
"""
import copy
import os
from typing import Optional

from xuloom.agents.builtin_agents import VERIFICATION_SYSTEM_PROMPT, VERIFICATION_CRITICAL_REMINDER


VERIFICATION_THRESHOLD = 3

_SKIP_PATTERNS = (
    ".xuloom", ".xuloom", ".git", "__pycache__", "node_modules",
    ".egg-info", ".tox", ".mypy_cache",
)


class VerificationManager:
    """Tracks file modifications and decides when to trigger verification."""

    def __init__(self):
        self.modified_files: dict[str, dict] = {}
        self.verification_history: list[dict] = []
        self._last_verified_set: set[str] = set()
        self.enabled = False

    def record_file_modification(self, path: str, operation: str = "edit"):
        abs_path = os.path.abspath(path)
        if abs_path not in self.modified_files:
            self.modified_files[abs_path] = {
                "path": abs_path,
                "operations": [],
                "count": 0,
            }
        entry = self.modified_files[abs_path]
        entry["operations"].append(operation)
        entry["count"] += 1

    def should_verify(self) -> bool:
        if not self.enabled:
            return False
        current_set = set(self.modified_files.keys())
        new_modifications = current_set - self._last_verified_set
        return len(new_modifications) >= VERIFICATION_THRESHOLD

    def get_files_to_verify(self) -> list[str]:
        current_set = set(self.modified_files.keys())
        new_files = current_set - self._last_verified_set
        filtered = [
            f for f in new_files
            if not any(pat in f for pat in _SKIP_PATTERNS)
        ]
        return sorted(filtered)

    def mark_verified(self, files: list[str], result: str):
        self._last_verified_set.update(files)
        self.verification_history.append({
            "files": files,
            "result_preview": result[:500] if result else "",
            "file_count": len(files),
        })
        if len(self.verification_history) > 10:
            self.verification_history = self.verification_history[-10:]

    def reset(self):
        self.modified_files.clear()
        self._last_verified_set.clear()

    def export_state(self) -> dict:
        return {
            "modified_files": copy.deepcopy(self.modified_files),
            "verification_history": copy.deepcopy(self.verification_history),
            "last_verified_set": list(self._last_verified_set),
            "enabled": self.enabled,
        }

    def load_state(self, payload: dict):
        payload = payload or {}
        self.modified_files = dict(payload.get("modified_files", {}))
        self.verification_history = list(payload.get("verification_history", []))
        self._last_verified_set = set(payload.get("last_verified_set", []))
        self.enabled = payload.get("enabled", True)


async def run_verification_agent(parent_engine, files_to_verify, sys_print_callback=None) -> Optional[str]:
    """Optional assessment through the same authorized, budgeted native worker API."""
    task = (VERIFICATION_SYSTEM_PROMPT + "\n" + VERIFICATION_CRITICAL_REMINDER
            + "\nAssess these artifacts against the user's requirements and evidence:\n"
            + "\n".join(str(path) for path in files_to_verify))
    receipt = await parent_engine.agents("spawn", subagent_type="verification", description="Independent assessment", prompt=task)
    if not receipt["ok"]:
        return "[Verification execution failed: " + receipt["error"]["message"] + "]"
    row = receipt["data"]["agents"][0]
    return str(row.get("result", "")) if row.get("status") == "completed" else "[Verification execution failed: " + str(row.get("status")) + "]\n" + str(row.get("result", ""))
