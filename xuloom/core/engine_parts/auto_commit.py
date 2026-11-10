"""Git auto-commit proposals and single-file undo."""
import asyncio
import copy
from xuloom.context.tool_results import build_tool_result
from xuloom.core.run_result import PersistenceError

try:
    import git
except ImportError:
    git = None


class AutoCommitMixin:
    """Git auto-commit proposals and single-file undo."""

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
