import copy
import os
import time


class ArtifactTracker:
    def __init__(self):
        self.attachments = []
        self.prefetch_history = []
        self.file_history = {}
        self.commit_history = []
        self.references = {}

    def _trim(self):
        self.attachments = self.attachments[:20]
        self.prefetch_history = self.prefetch_history[:40]
        self.commit_history = self.commit_history[:20]
        for path, items in list(self.file_history.items()):
            self.file_history[path] = items[:10]
            if not self.file_history[path]:
                del self.file_history[path]

    def record_attachment(
        self,
        *,
        path: str,
        kind: str,
        source: str,
        agent_id: str = None,
        session_id: str = None,
        metadata: dict = None,
    ):
        self.attachments.insert(0, {
            "path": os.path.abspath(path),
            "kind": kind,
            "source": source,
            "agent_id": agent_id,
            "session_id": session_id,
            "metadata": copy.deepcopy(metadata or {}),
            "timestamp": time.time(),
        })
        self._trim()

    def record_prefetch(
        self,
        *,
        path: str,
        kind: str,
        source: str,
        start_line=None,
        end_line=None,
        cache_hit: bool = False,
        chars: int = 0,
        agent_id: str = None,
        session_id: str = None,
    ):
        self.prefetch_history.insert(0, {
            "path": os.path.abspath(path),
            "kind": kind,
            "source": source,
            "start_line": start_line,
            "end_line": end_line,
            "cache_hit": cache_hit,
            "chars": int(chars or 0),
            "agent_id": agent_id,
            "session_id": session_id,
            "timestamp": time.time(),
        })
        self._trim()

    def record_file_change(
        self,
        *,
        path: str,
        operation: str,
        before_snapshot: dict = None,
        after_snapshot: dict = None,
        target_preview: str = "",
        replacement_preview: str = "",
        agent_id: str = None,
        session_id: str = None,
    ):
        abs_path = os.path.abspath(path)
        history = self.file_history.setdefault(abs_path, [])
        history.insert(0, {
            "operation": operation,
            "before": copy.deepcopy(before_snapshot),
            "after": copy.deepcopy(after_snapshot),
            "target_preview": target_preview[:240],
            "replacement_preview": replacement_preview[:240],
            "agent_id": agent_id,
            "session_id": session_id,
            "timestamp": time.time(),
        })
        self._trim()

    def record_commit(
        self,
        *,
        cwd: str,
        command: str,
        success: bool,
        status: str = "",
        commit_hash: str = "",
        message_preview: str = "",
        head_before: str = "",
        head_after: str = "",
        branch: str = "",
        repo_root: str = "",
        author_name: str = "",
        author_email: str = "",
        tool_name: str = "",
        agent_role: str = "",
        parent_agent_id: str = None,
        agent_depth: int = None,
        exit_code: int = None,
        agent_id: str = None,
        session_id: str = None,
    ):
        self.commit_history.insert(0, {
            "cwd": os.path.abspath(cwd or os.getcwd()),
            "command": command,
            "success": bool(success),
            "status": status or ("created" if success and commit_hash else "failed"),
            "commit_hash": commit_hash,
            "message_preview": message_preview[:240],
            "head_before": head_before,
            "head_after": head_after or commit_hash,
            "branch": branch,
            "repo_root": os.path.abspath(repo_root) if repo_root else "",
            "author_name": author_name,
            "author_email": author_email,
            "tool_name": tool_name,
            "agent_role": agent_role,
            "parent_agent_id": parent_agent_id,
            "agent_depth": agent_depth,
            "exit_code": exit_code,
            "agent_id": agent_id,
            "session_id": session_id,
            "timestamp": time.time(),
        })
        self._trim()

    def export_state(self):
        return {
            "attachments": copy.deepcopy(self.attachments),
            "prefetch_history": copy.deepcopy(self.prefetch_history),
            "file_history": copy.deepcopy(self.file_history),
            "commit_history": copy.deepcopy(self.commit_history),
            "references": copy.deepcopy(self.references),
        }

    def load_state(self, payload):
        payload = payload or {}
        self.attachments = list(payload.get("attachments", []) or [])
        self.prefetch_history = list(payload.get("prefetch_history", []) or [])
        self.file_history = dict(payload.get("file_history", {}) or {})
        self.commit_history = list(payload.get("commit_history", []) or [])
        self.references = {}
        from xuloom.features.session_summary import merge_references
        merge_references(self.references, payload.get("references", {}))
        self._trim()

    def render_summary(self) -> str:
        file_history_count = sum(len(items) for items in self.file_history.values())
        return (
            f"Artifacts: attachments={len(self.attachments)}, "
            f"prefetches={len(self.prefetch_history)}, "
            f"file_changes={file_history_count}, "
            f"commits={len(self.commit_history)}"
        )
