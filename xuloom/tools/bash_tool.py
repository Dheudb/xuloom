import os
from xuloom.utils.shell import decode_output, process_options, shell_guidance, terminate_process_tree
import asyncio
import re
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result

class BashToolInput(BaseModel):
    command: str = Field(..., description="The command to execute")
    timeout: int = Field(None, description="Optional timeout in milliseconds (max 600000)")
    description: str = Field(None, description="Clear, concise description of what this command does in active voice.")
    run_in_background: bool = Field(
        False,
        description="Set to true to run this command in the background. Use file_read_tool to read the output later.",
    )

class BashTool(BaseAgenticTool):
    name = "bash_tool"
    description = "Runs a native shell command (Windows cmd.exe on Windows, /bin/sh on Unix), not necessarily Bash. Prefer command_tool argv/stdin for exact arguments."
    input_schema = BashToolInput
    risk_level = "high"

    def _artifact_tracker(self):
        return self.context.get("artifact_tracker")

    def _looks_like_git_commit(self, command: str) -> bool:
        return "git commit" in str(command or "").lower()

    def _extract_commit_message_preview(self, command: str) -> str:
        rendered = str(command or "")
        patterns = [
            r"-m\s+\"([^\"]+)\"",
            r"-m\s+'([^']+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, rendered)
            if match:
                return match.group(1)
        return rendered[:240]

    async def _run_git_capture(self, git_command: str, cwd: str = None, timeout: float = 10.0) -> str:
        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                git_command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options(),
            )
            stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode == 0 and stdout_bytes:
                return decode_output(stdout_bytes).strip()
        except asyncio.CancelledError:
            await asyncio.shield(terminate_process_tree(process))
            raise
        except Exception:
            await terminate_process_tree(process)
            return ""
        return ""

    async def _resolve_head_commit(self, cwd: str = None) -> str:
        return await self._run_git_capture("git rev-parse HEAD", cwd=cwd)

    async def _resolve_repo_root(self, cwd: str = None) -> str:
        return await self._run_git_capture("git rev-parse --show-toplevel", cwd=cwd)

    async def _resolve_branch_name(self, cwd: str = None) -> str:
        return await self._run_git_capture("git branch --show-current", cwd=cwd)

    async def _resolve_commit_details(self, commit_hash: str, cwd: str = None) -> dict:
        if not commit_hash:
            return {}
        raw = await self._run_git_capture(
            f'git log -1 --format="%s%n%an%n%ae" {commit_hash}',
            cwd=cwd,
        )
        if not raw:
            return {}
        lines = raw.splitlines()
        return {
            "message_preview": (lines[0] if len(lines) > 0 else "")[:240],
            "author_name": lines[1] if len(lines) > 1 else "",
            "author_email": lines[2] if len(lines) > 2 else "",
        }

    async def _build_commit_attribution(self, command: str, cwd: str = None) -> dict:
        repo_root = await self._resolve_repo_root(cwd=cwd)
        branch = await self._resolve_branch_name(cwd=cwd)
        head_before = await self._resolve_head_commit(cwd=cwd)
        return {
            "repo_root": repo_root,
            "branch": branch,
            "head_before": head_before,
            "message_preview": self._extract_commit_message_preview(command),
        }

    def prompt(self) -> str:
        return shell_guidance() + """IMPORTANT: Avoid using bash_tool to run cat, head, tail, sed, awk, find, grep, or echo commands unless explicitly instructed. Instead use the appropriate dedicated tool:
 - File search: Use glob_tool (NOT find or ls)
 - Content search: Use grep_tool (NOT grep or rg)
 - Read files: Use file_read_tool (NOT cat/head/tail)
 - Edit files: Use file_edit_tool (NOT sed/awk)
 - Write files: Use file_write_tool (NOT echo >/cat <<EOF)
 - Communication: Output text directly (NOT echo/printf)
Reserve bash_tool exclusively for system commands and terminal operations that require shell execution. While bash_tool can do similar things, it's better to use the built-in tools as they provide a better user experience and make it easier to review tool calls.

# Instructions
 - If your command will create new directories or files, first run `ls` to verify the parent directory exists.
 - Always quote file paths that contain spaces with double quotes.
 - Try to maintain your current working directory by using absolute paths and avoiding `cd`.
 - You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). By default, commands timeout after 120000ms (2 minutes).
 - You can use the `run_in_background` parameter to run the command in the background. Only use this if you don't need the result immediately. You will be notified when it finishes. You do not need to use '&' at the end of the command when using this parameter.
 - When issuing multiple commands:
   - If independent, make multiple bash_tool calls in parallel.
   - If dependent, chain with '&&' in a single call.
   - Use ';' only when you need sequential execution but don't care if earlier commands fail.
   - DO NOT use newlines to separate commands.
 - Avoid unnecessary `sleep` commands:
   - Do not sleep between commands that can run immediately — just run them.
   - If your command is long running, use `run_in_background`. No sleep needed.
   - Do not retry failing commands in a sleep loop — diagnose the root cause.
   - If you must sleep, keep the duration short (1-5 seconds) to avoid blocking the user.

# Git operations
 - NEVER update the git config.
 - NEVER run destructive git commands (push --force, reset --hard, checkout ., clean -f, branch -D) unless the user explicitly requests them. Taking unauthorized destructive actions can result in lost work.
 - NEVER skip hooks (--no-verify, --no-gpg-sign) unless the user explicitly requests it.
 - NEVER force push to main/master. Warn the user if they request it.
 - CRITICAL: Always create NEW commits rather than amending, unless the user explicitly requests amend. When a pre-commit hook fails, the commit did NOT happen — so --amend would modify the PREVIOUS commit, destroying work. Fix the issue, re-stage, and create a NEW commit.
 - When staging files, prefer adding specific files by name rather than 'git add -A' or 'git add .' which can accidentally include sensitive files (.env, credentials).
 - NEVER commit changes unless the user explicitly asks. Only commit when explicitly asked.
 - Do not commit files that likely contain secrets (.env, credentials.json, etc). Warn the user if they specifically request to commit those files.
 - When creating commits, pass the message via HEREDOC for formatting.
 - NEVER use git commands with -i flag (interactive mode not supported).
 - NEVER use --no-edit with git rebase commands.
 - If there are no changes to commit, do not create an empty commit.
 - Use gh command for GitHub tasks (issues, PRs, checks, releases). If given a Github URL, use gh to get info.

# Committing changes with git
When the user asks you to create a new git commit:
1. Run in parallel: git status (never -uall flag), git diff, git log --oneline -n 5
2. Analyze changes, draft a concise commit message focusing on "why" not "what"
3. Add specific files, commit with HEREDOC message, run git status after
4. If pre-commit hook fails: fix issue, create NEW commit (never amend)

# Creating pull requests
1. Run in parallel: git status, git diff, check remote tracking, git log + git diff [base]...HEAD
2. Analyze ALL commits (not just latest), draft title (<70 chars) + summary
3. Push with -u flag, create PR with gh pr create using HEREDOC body
4. Return the PR URL when done"""

    def validate_input(
        self,
        command: str = None,
        timeout: int = None,
        description: str = None,
        run_in_background: bool = False,
    ):
        if not command:
            return "command is required."
        if timeout is not None and timeout > 600000:
            return "timeout cannot exceed 600000ms (10 minutes)."
        return None

    def is_read_only_call(
        self,
        command: str = None,
        timeout: int = None,
        description: str = None,
        run_in_background: bool = False,
    ) -> bool:
        return False

    def build_permission_summary(
        self,
        command: str = None,
        timeout: int = None,
        description: str = None,
        run_in_background: bool = False,
    ) -> str:
        return (
            "Shell command execution requested.\n"
            f"command: {command or '<none>'}\n"
            f"description: {description or '<none>'}\n"
            f"run_in_background: {run_in_background}"
        )
    
    async def execute(
        self,
        command: str = None,
        timeout: int = None,
        description: str = None,
        run_in_background: bool = False,
    ) -> dict:
        task_manager = self.context.get("shell_task_manager")
        cwd = self.context.get("session_cwd")
        sandbox = self.context.get("sandbox_manager")
        if sandbox and sandbox.active_sandbox:
            cwd = sandbox.resolve_working_directory(cwd or os.getcwd())

        if not command:
            return build_tool_result(
                ok=False,
                content="command is required.",
                metadata={},
                is_error=True,
            )

        if run_in_background:
            if not task_manager:
                return build_tool_result(
                    ok=False,
                    content="Shell task manager is unavailable.",
                    metadata={"command": command},
                    is_error=True,
                )
            record = await task_manager.start_task(command, cwd=cwd, owner=self.context.get("agent_id"))
            return build_tool_result(
                ok=True,
                content=f"Background task started: {record['task_id']}",
                metadata={"run_in_background": True, **record},
            )

        effective_timeout = min((timeout or 120000), 600000) / 1000.0
        process = None
        try:
            commit_context = {}
            if self._looks_like_git_commit(command):
                commit_context = await self._build_commit_attribution(command, cwd=cwd)

            env = os.environ.copy()
            env["MPLBACKEND"] = "Agg"

            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                **process_options(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
            
            output = ""
            if stdout_bytes:
                output += decode_output(stdout_bytes)
            if stderr_bytes:
                error_str = decode_output(stderr_bytes)
                if error_str.strip():
                    output += f"\n--- STDERR ---\n{error_str}"

            commit_metadata = {}
            if self._looks_like_git_commit(command):
                head_commit = await self._resolve_head_commit(cwd=cwd)
                commit_details = await self._resolve_commit_details(head_commit, cwd=cwd)
                head_before = commit_context.get("head_before", "")
                message_preview = (
                    commit_details.get("message_preview")
                    or commit_context.get("message_preview")
                    or self._extract_commit_message_preview(command)
                )
                commit_created = bool(process.returncode == 0 and head_commit and head_commit != head_before)
                commit_status = "created" if commit_created else ("no_commit_created" if process.returncode == 0 else "failed")
                tracker = self._artifact_tracker()
                commit_metadata = {
                    "agent_created_commit": commit_created,
                    "commit_hash": head_commit,
                    "commit_message_preview": message_preview,
                    "commit_head_before": head_before,
                    "commit_head_after": head_commit,
                    "commit_branch": commit_context.get("branch", ""),
                    "commit_repo_root": commit_context.get("repo_root", ""),
                    "commit_status": commit_status,
                    "commit_author_name": commit_details.get("author_name", ""),
                    "commit_author_email": commit_details.get("author_email", ""),
                }
                if tracker is not None:
                    tracker.record_commit(
                        cwd=cwd or os.getcwd(),
                        command=command,
                        success=process.returncode == 0,
                        status=commit_status,
                        commit_hash=head_commit,
                        message_preview=message_preview,
                        head_before=head_before,
                        head_after=head_commit,
                        branch=commit_context.get("branch", ""),
                        repo_root=commit_context.get("repo_root", ""),
                        author_name=commit_details.get("author_name", ""),
                        author_email=commit_details.get("author_email", ""),
                        tool_name=self.name,
                        agent_role=self.context.get("agent_role"),
                        parent_agent_id=self.context.get("parent_agent_id"),
                        agent_depth=self.context.get("agent_depth"),
                        exit_code=process.returncode,
                        agent_id=self.context.get("agent_id"),
                        session_id=self.context.get("session_id"),
                    )

            if not output.strip():
                return build_tool_result(
                    ok=process.returncode == 0,
                    content="Command execution completed but produced no output.",
                    metadata={
                        "exit_code": process.returncode,
                        "cwd": cwd or os.getcwd(),
                        "command": command,
                        **commit_metadata,
                    },
                    is_error=process.returncode != 0,
                )
                
            # The common serializer archives the full result and supplies a read cursor.
            truncated = False

            return build_tool_result(
                ok=process.returncode == 0,
                content=output,
                metadata={
                    "exit_code": process.returncode,
                    "cwd": cwd or os.getcwd(),
                    "command": command,
                    "truncated": truncated,
                    **commit_metadata,
                },
                is_error=process.returncode != 0,
            )
            
        except asyncio.CancelledError:
            await asyncio.shield(terminate_process_tree(process))
            raise
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            if self._looks_like_git_commit(command):
                commit_context = await self._build_commit_attribution(command, cwd=cwd)
                tracker = self._artifact_tracker()
                if tracker is not None:
                    tracker.record_commit(
                        cwd=cwd or os.getcwd(),
                        command=command,
                        success=False,
                        status="timeout",
                        commit_hash="",
                        message_preview=commit_context.get("message_preview", self._extract_commit_message_preview(command)),
                        head_before=commit_context.get("head_before", ""),
                        head_after="",
                        branch=commit_context.get("branch", ""),
                        repo_root=commit_context.get("repo_root", ""),
                        tool_name=self.name,
                        agent_role=self.context.get("agent_role"),
                        parent_agent_id=self.context.get("parent_agent_id"),
                        agent_depth=self.context.get("agent_depth"),
                        exit_code=None,
                        agent_id=self.context.get("agent_id"),
                        session_id=self.context.get("session_id"),
                    )
            return build_tool_result(
                ok=False,
                content=f"Command '{command}' timed out after {effective_timeout:.0f} seconds and was forcefully killed.",
                metadata={
                    "command": command,
                    "cwd": cwd or os.getcwd(),
                    "timeout_ms": timeout or 120000,
                },
                is_error=True,
            )
        except Exception as e:
            if self._looks_like_git_commit(command):
                commit_context = await self._build_commit_attribution(command, cwd=cwd)
                tracker = self._artifact_tracker()
                if tracker is not None:
                    tracker.record_commit(
                        cwd=cwd or os.getcwd(),
                        command=command,
                        success=False,
                        status="error",
                        commit_hash="",
                        message_preview=commit_context.get("message_preview", self._extract_commit_message_preview(command)),
                        head_before=commit_context.get("head_before", ""),
                        head_after="",
                        branch=commit_context.get("branch", ""),
                        repo_root=commit_context.get("repo_root", ""),
                        tool_name=self.name,
                        agent_role=self.context.get("agent_role"),
                        parent_agent_id=self.context.get("parent_agent_id"),
                        agent_depth=self.context.get("agent_depth"),
                        exit_code=None,
                        agent_id=self.context.get("agent_id"),
                        session_id=self.context.get("session_id"),
                    )
            return build_tool_result(
                ok=False,
                content=f"Error executing command: {str(e)}",
                metadata={
                    "command": command,
                    "cwd": cwd or os.getcwd(),
                },
                is_error=True,
            )
