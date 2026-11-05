"""
Attachment auto-injection system.

Generates contextual attachments that are injected as system-reminder
messages before each model turn. Mirrors Claude Code's utils/attachments.ts.

Attachment types:
  - file_change_delta: diffs of files modified since last turn
  - plan_mode_reminder: plan mode workflow instructions
  - todo_reminder: nudge to use todo_write_tool for complex tasks
  - agent_listing: available agent types for agent_tool
  - diagnostics: compiler/linter warnings from LSP
  - skill_recommendation: auto-surfaced relevant skills
"""

import os
import time
from typing import Dict, List, Optional


SYSTEM_REMINDER_OPEN = "<system-reminder>"
SYSTEM_REMINDER_CLOSE = "</system-reminder>"


def _wrap_reminder(content: str) -> str:
    return f"{SYSTEM_REMINDER_OPEN}\n{content}\n{SYSTEM_REMINDER_CLOSE}"


class AttachmentCollector:
    """
    Collects and formats contextual attachments to inject before each turn.
    Instantiated once per engine and refreshed at each turn boundary.
    """

    def __init__(self):
        self._last_file_snapshots: Dict[str, str] = {}
        self._last_turn_time: float = 0
        self._plan_mode_injected: bool = False
        self._todo_reminder_cooldown: int = 0
        self._recent_tool_names: List[str] = []
        self._skill_recommended_at_turn: int = -10
        self._skill_cache: Optional[List[dict]] = None

    def collect_attachments(
        self,
        *,
        cwd: str,
        plan_mode: str = "normal",
        plan_content: str = "",
        plan_file_path: str = "",
        plan_exists: bool = False,
        todo_summary: str = "",
        todo_count: int = 0,
        active_tools: List[str] = None,
        changed_files: Optional[List[str]] = None,
        lsp_diagnostics: str = "",
        turn_count: int = 0,
        recent_tool_names: Optional[List[str]] = None,
        user_query: str = "",
        skill_roots: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        Build all attachment messages for the current turn.
        Returns a list of user-role messages with system-reminder content.
        """
        attachments = []

        diff_att = self._build_file_change_delta(cwd, changed_files)
        if diff_att:
            attachments.append(diff_att)

        plan_att = self._build_plan_mode_reminder(
            plan_mode, plan_content, turn_count,
            plan_file_path=plan_file_path,
            plan_exists=plan_exists,
        )
        if plan_att:
            attachments.append(plan_att)

        todo_att = self._build_todo_reminder(
            todo_summary, todo_count, turn_count, recent_tool_names or []
        )
        if todo_att:
            attachments.append(todo_att)

        if lsp_diagnostics:
            attachments.append(self._build_diagnostics_attachment(lsp_diagnostics))

        skill_att = self._build_skill_recommendation(
            cwd, user_query, turn_count, skill_roots
        )
        if skill_att:
            attachments.append(skill_att)

        self._last_turn_time = time.time()
        return attachments

    def _build_file_change_delta(
        self, cwd: str, changed_files: Optional[List[str]]
    ) -> Optional[dict]:
        """Generate a diff summary for files that changed since the last turn."""
        if not changed_files:
            return None

        diffs = []
        inspected = 0
        for fpath in changed_files:
            abs_path = fpath if os.path.isabs(fpath) else os.path.join(cwd, fpath)
            try:
                relative = os.path.relpath(abs_path, cwd)
            except ValueError:
                relative = abs_path
            if relative.replace("\\", "/").split("/")[0].lower() in {".xuloom", ".git", "mcp_servers.json"}:
                continue
            if inspected >= 10:
                break
            inspected += 1
            if not os.path.isfile(abs_path):
                diffs.append(f"--- {fpath}\n(file deleted or moved)")
                if fpath in self._last_file_snapshots:
                    del self._last_file_snapshots[fpath]
                continue

            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    current = f.read(50000)
            except Exception:
                continue

            prev = self._last_file_snapshots.get(fpath)
            if prev is not None and prev == current:
                continue

            self._last_file_snapshots[fpath] = current

            if prev is None:
                diffs.append(f"--- {fpath}\n(file newly tracked, {len(current)} chars)")
            else:
                diff_text = self._simple_diff(prev, current, fpath)
                if diff_text:
                    diffs.append(diff_text)

        if not diffs:
            return None

        content = _wrap_reminder(
            "Files changed since your last turn:\n\n" + "\n\n".join(diffs[:5])
        )
        return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "file_change_delta"}

    def _simple_diff(self, old: str, new: str, path: str, context: int = 3) -> str:
        """Generate a minimal unified-style diff summary."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        if old_lines == new_lines:
            return ""

        try:
            import difflib
            diff = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{path}", tofile=f"b/{path}",
                n=context,
            ))
        except Exception:
            return f"--- {path}\n(changed, diff unavailable)"

        if not diff:
            return ""

        diff_text = "".join(diff[:100])
        if len(diff) > 100:
            diff_text += f"\n... ({len(diff) - 100} more diff lines truncated)"
        return diff_text

    def _build_plan_mode_reminder(
        self,
        plan_mode: str,
        plan_content: str,
        turn_count: int,
        plan_file_path: str = "",
        plan_exists: bool = False,
    ) -> Optional[dict]:
        """Inject plan mode instructions — full on first injection, sparse after."""
        if plan_mode != "plan":
            if self._plan_mode_injected:
                plan_ref = ""
                if plan_file_path and plan_exists:
                    plan_ref = f" The plan file is located at {plan_file_path} if you need to reference it."
                content = _wrap_reminder(
                    "## Exited Plan Mode\n\n"
                    f"You have exited plan mode. You can now make edits, run tools, and take actions.{plan_ref}"
                )
                self._plan_mode_injected = False
                return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "plan_mode_exit"}
            return None

        if self._plan_mode_injected and (turn_count % 5 != 0):
            content = _wrap_reminder(
                f"Plan mode still active (see full instructions earlier in conversation). "
                f"Read-only except plan file at {plan_file_path}. "
                f"End turns with ask_user_question_tool (for clarifications) or exit_plan_mode (for plan approval). "
                f"Never ask about plan approval via text or ask_user_question_tool."
            )
            return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "plan_mode_sparse"}

        self._plan_mode_injected = True

        if plan_exists:
            plan_file_info = (
                f"A plan file already exists at {plan_file_path}. "
                f"You can read it and make incremental edits using the file_edit_tool."
            )
        else:
            plan_file_info = (
                f"No plan file exists yet. You should create your plan at {plan_file_path} "
                f"using the file_write_tool."
            )

        lines = [
            "Plan mode is active. The user indicated that they do not want you to execute yet "
            "-- you MUST NOT make any edits (with the exception of the plan file mentioned below), "
            "run any non-readonly tools (including changing configs or making commits), or otherwise "
            "make any changes to the system. This supercedes any other instructions you have received.",
            "",
            "## Plan File Info:",
            plan_file_info,
            "You should build your plan incrementally by writing to or editing this file. "
            "NOTE that this is the only file you are allowed to edit — other than this you are "
            "only allowed to take READ-ONLY actions.",
            "",
            "## Plan Workflow",
            "",
            "### Phase 1: Initial Understanding & Research",
            "Goal: Gain a comprehensive understanding of the user's request through exploration, "
            "research, and clarification only when needed for this goal.",
            "Use relevant supplied material and sources; do not assume a repository or workspace search is needed.",
            "Delegate only useful independent investigations, never to meet a minimum agent count.",
            "",
            "### Final Plan",
            f"Goal: Write your final plan to the plan file (the only file you can edit: {plan_file_path}).",
            "Include the user's goal, constraints, relevant evidence, proposed steps and checks appropriate to the domain.",
            "Distinguish confirmed facts, assumptions and unresolved dependencies. Keep the plan proportionate to the task.",
            "",
            "### Call exit_plan_mode",
            "At the end of your turn, once needed clarifications are resolved and you are happy "
            "with your final plan file — you should always call exit_plan_mode to indicate to the "
            "user that you are done planning.",
            "This is critical — your turn should only end with either using ask_user_question_tool "
            "OR calling exit_plan_mode. Do not stop unless it's for these 2 reasons.",
            "",
            "**Important:** Use ask_user_question_tool ONLY to clarify requirements or choose between "
            "approaches. Use exit_plan_mode to request plan approval. Do NOT ask about plan approval "
            "in any other way — no text questions, no ask_user_question_tool. Phrases like "
            "\"Is this plan okay?\", \"Should I proceed?\" MUST use exit_plan_mode.",
            "",
            "NOTE: At any point in time through this workflow you should feel free to ask the user "
            "questions or clarifications using ask_user_question_tool. Don't make large assumptions "
            "about user intent.",
        ]

        content = _wrap_reminder("\n".join(lines))
        return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "plan_mode_reminder"}

    def _build_todo_reminder(
        self,
        todo_summary: str,
        todo_count: int,
        turn_count: int,
        recent_tool_names: List[str],
    ) -> Optional[dict]:
        """Nudge the model to use todo_write_tool for complex multi-step tasks."""
        if self._todo_reminder_cooldown > 0:
            self._todo_reminder_cooldown -= 1
            return None

        if todo_count > 0:
            return None

        if turn_count < 3:
            return None

        has_used_todo = "todo_write_tool" in recent_tool_names
        if has_used_todo:
            return None

        multi_step_signals = sum(1 for t in recent_tool_names[-10:] if t in {
            "file_edit_tool", "file_write_tool", "bash_tool", "file_read_tool"
        })
        if multi_step_signals < 3:
            return None

        self._todo_reminder_cooldown = 5

        content = _wrap_reminder(
            "You are working on a multi-step task but have not created a todo list. "
            "Consider using todo_write_tool to track your progress — this helps the user "
            "understand what you're doing and ensures you don't miss steps."
        )
        return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "todo_reminder"}

    def _build_skill_recommendation(
        self,
        cwd: str,
        user_query: str,
        turn_count: int,
        skill_roots: Optional[List[str]] = None,
    ) -> Optional[dict]:
        """
        Surface relevant skills based on the user's query keywords.
        Only triggers once every 8 turns to avoid spam.
        """
        if not user_query or turn_count - self._skill_recommended_at_turn < 8:
            return None

        self._skill_cache = self._discover_available_skills(cwd, skill_roots)

        if not self._skill_cache:
            return None

        query_lower = user_query.lower()
        query_words = set(query_lower.split())
        relevant = []
        for skill in self._skill_cache:
            name_lower = skill["name"].lower()
            keywords = set(skill.get("keywords", []))
            if name_lower in query_lower:
                relevant.append(skill)
            elif query_words & keywords:
                relevant.append(skill)

        if not relevant:
            return None

        self._skill_recommended_at_turn = turn_count

        lines = [
            "Relevant skills detected for the current task. "
            "Consider using `skill_tool` with action='read' to load them:",
        ]
        for s in relevant[:5]:
            lines.append(f"  - {s['name']}: {s['path']}")

        content = _wrap_reminder("\n".join(lines))
        return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "skill_recommendation"}

    def _discover_available_skills(
        self, cwd: str, extra_roots: Optional[List[str]] = None
    ) -> List[dict]:
        """Scan filesystem for SKILL.md files and extract keywords."""
        from xuloom.skills.registry import SkillRegistry
        return [{**skill, "keywords": self._extract_skill_keywords(skill["path"], skill["name"])} for skill in SkillRegistry(cwd).active()]

    @staticmethod
    def _extract_skill_keywords(skill_path: str, skill_name: str) -> List[str]:
        """Extract searchable keywords from a SKILL.md file header."""
        keywords = set()
        for word in skill_name.lower().replace("-", " ").replace("_", " ").split():
            if len(word) >= 3:
                keywords.add(word)
        try:
            with open(skill_path, "r", encoding="utf-8", errors="replace") as f:
                header = f.read(500).lower()
            for word in header.split():
                cleaned = word.strip(".,;:!?()[]{}#*`\"'")
                if len(cleaned) >= 4 and cleaned.isalpha():
                    keywords.add(cleaned)
        except Exception:
            pass
        return list(keywords)[:20]

    def _build_diagnostics_attachment(self, diagnostics: str) -> dict:
        content = _wrap_reminder(
            f"Compiler/linter diagnostics detected:\n{diagnostics}"
        )
        return {"role": "user", "content": content, "_is_meta": True, "_attachment_type": "diagnostics"}

    def track_tool_use(self, tool_name: str):
        """Record tool usage for reminder heuristics."""
        self._recent_tool_names.append(tool_name)
        if len(self._recent_tool_names) > 50:
            self._recent_tool_names = self._recent_tool_names[-30:]



    def export_state(self) -> dict:
        return {
            "last_turn_time": self._last_turn_time,
            "todo_reminder_cooldown": self._todo_reminder_cooldown,
            "tracked_file_count": len(self._last_file_snapshots),
            "recent_tool_count": len(self._recent_tool_names),
        }
