"""Collaboration, plan and coordinator modes plus Ultraplan entry points."""
import copy
from xuloom.core.engine_state import EngineState


class ModesMixin(EngineState):
    """Collaboration, plan and coordinator modes plus Ultraplan entry points."""

    def set_collaboration_mode(self, mode, *, persist=True):
        from xuloom.agents.collaboration import policy, new_run
        active = getattr(self, "active_run_task", None)
        workers = self.tool_context.get("subagent_tasks")
        if (active and not active.done()) or (workers and any(not task.done() for task in workers.tasks.values())):
            raise ValueError("请先停止或等待当前任务，再切换协作档位")
        profile = policy(mode, self.tool_context["skill_registry"].settings())
        if persist:
            self.require_writer()
        previous = (self.collaboration_mode, self.tool_context.get("collaboration"), self.tool_context.get("collaboration_run"))
        self.collaboration_mode = mode
        self.tool_context["collaboration"] = profile
        self.tool_context["collaboration_run"] = new_run(profile)
        if persist and not self.persist_session_state():
            self.collaboration_mode, self.tool_context["collaboration"], self.tool_context["collaboration_run"] = previous
            raise OSError("协作档位保存失败")
        return copy.deepcopy(profile)

    def _collaboration_instructions(self) -> str:
        from xuloom.agents.collaboration import instructions
        from xuloom.agents.ultra_execution import required
        return instructions(self.tool_context["collaboration"],assessment=required(self))

    def _build_tool_prompt_summary(self) -> str:
        from xuloom.core.continuation import PROTOCOL
        policy = self.permission_manager.approval_policy.mode
        sections = [("Full access: do not ask for duplicate tool approval; role/Plan restrictions still apply."
            if policy == "full" else "The runtime requests tool approval when required; do not duplicate it with a question.")]
        sections.append(PROTOCOL)
        if self.execution_sandbox.enabled:
            sections.append("System workspace isolation: file tools, command_tool and repl_tool run in restricted OS processes. "
                "Network access and unconfined MCP/browser/LSP/legacy shell tools are blocked. Do not attempt host fallbacks. "
                "Only the authorized working directory is writable; runtime libraries are read-only. Approval cannot bypass isolation.")
        if self.tool_context.get("agent_system_prompt"):
            sections.append("# Assigned agent role\n" + self.tool_context["agent_system_prompt"])
        if self.tool_context.get("workspace_moves"):
            sections.append("# Conversation working directory\nThis conversation has moved between projects. "
                            "Its current working directory is " + self.session_cwd + ". "
                            "Historical paths and project instructions may belong to earlier locations. "
                            "Use the current directory and its project instructions for new tasks; do not reuse old project paths by default.")
        return "\n".join(sections)

    def set_mode(self, mode: str) -> str:
        return self.plan_manager.set_mode(mode)

    def get_mode(self) -> str:
        return self.plan_manager.get_mode()

    # --- Coordinator mode ---

    COORDINATOR_ALLOWED_TOOLS = {
        "agent_tool", "spawn_agent", "send_message_tool", "team_create_tool",
        "team_delete_tool", "plan_tool", "todo_write_tool",
        "file_read_tool", "grep_tool", "glob_tool",
    }

    PLAN_MODE_ALLOWED_TOOLS = {
        "file_read_tool", "grep_tool", "glob_tool",
        "plan_tool", "todo_write_tool", "agent_tool", "spawn_agent",
        "web_search_tool", "web_fetch_tool",
        "lsp_tool", "tool_search_tool",
    }

    PLAN_MODE_BLOCKED_TOOLS = {
        "bash_tool", "repl_tool",
        "notebook_tool", "notebook_edit_tool", "sandbox_tool",
        "task_create_tool", "task_kill_tool",
    }

    def enable_coordinator_mode(self):
        self.activate_tools(["team_create_tool", "team_delete_tool", "send_message_tool"])
        self.coordinator_mode = True

    def disable_coordinator_mode(self):
        self.coordinator_mode = False

    def _get_effective_tools(self) -> dict:
        """Return tool set filtered by current mode (plan / coordinator)."""
        tools = self.available_tools
        if self.execution_sandbox.enabled:
            from xuloom.security.execution import FILE_TOOLS, CONTROL_TOOLS, PROCESS_TOOLS
            tools = {name: tool for name, tool in tools.items() if name in FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS}
        if self.coordinator_mode:
            return {
                name: tool for name, tool in tools.items()
                if name in self.COORDINATOR_ALLOWED_TOOLS or name in {'ultra_answer','ultra_prepare'}
            }
        if self.plan_manager.get_mode() == "plan":
            return {
                name: tool for name, tool in tools.items()
                if name not in self.PLAN_MODE_BLOCKED_TOOLS
            }
        return tools

    def _get_coordinator_system_prompt_addon(self) -> str:
        if not self.coordinator_mode:
            return ""
        return (
            "\n\n--- COORDINATOR MODE ACTIVE ---\n"
            "You are operating as a **coordinator agent**. Your role:\n"
            "1. Break complex tasks into subtasks and delegate to workers via agent_tool.\n"
            "2. Use send_message_tool to send follow-up instructions to running workers.\n"
            "3. Track progress with todo_write_tool and plan_tool.\n"
            "4. You should NOT directly modify code/files — delegate that to workers.\n"
            "5. Synthesize worker results into a coherent final answer.\n"
            "Available worker-facing tools: agent_tool, send_message_tool\n"
            "Available planning tools: plan_tool, todo_write_tool\n"
            "Available read-only tools: file_read_tool, grep_tool, glob_tool\n"
        )

    def get_team_summary(self) -> str:
        return self.team_manager.get_summary()

    # --- Ultraplan ---

    async def _run_ultraplan_worker(self, description, seed_plan=""):
        receipt = await self.agents("spawn", subagent_type="plan", description="Deep planning",
            prompt="Develop a complete plan for the following task. Return the full plan as your final answer; do not write files. "
                   "Include constraints, dependencies, risks and acceptance criteria appropriate to the task.\nTask:\n" + description
                   + ("\nExisting draft:\n" + seed_plan if seed_plan else ""))
        if not receipt["ok"]:
            raise RuntimeError(receipt["error"]["message"])
        row = receipt["data"]["agents"][0]
        if row.get("status") != "completed":
            raise RuntimeError("Planning execution failed: " + str(row.get("status")) + ": " + str(row.get("result", "")))
        return row["result"]

    async def launch_ultraplan(self, description: str, seed_plan: str = "", on_phase_change=None) -> dict:
        result = await self.ultraplan_manager.launch(
            description=description,
            runner=self._run_ultraplan_worker,
            seed_plan=seed_plan,
            on_phase_change=on_phase_change,
        )
        if result.get("ok") and result.get("plan"):
            self.plan_manager.write_plan(result["plan"])
            self.set_mode("normal")
            self.persist_session_state()
        return result

    def get_ultraplan_summary(self) -> str:
        return self.ultraplan_manager.get_status_summary()
