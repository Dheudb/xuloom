"""The built-in tool set shared by every QueryEngine.

``default_tools()`` returns fresh instances keyed by tool name. Tools whose
names are in ``LATENT_TOOL_NAMES`` are installed but hidden from the model
until activated through ``tool_search_tool`` (or ``engine.activate_tools``).
Hosts can drop built-ins with ``EngineConfig(disabled_tools={...})``.
"""
from xuloom.agents.agent_tool import AgentTool, SpawnAgentTool
from xuloom.skills.skill_tool import SkillTool
from xuloom.tools.ask_user_question_tool import AskUserQuestionTool
from xuloom.tools.bash_tool import BashTool
from xuloom.tools.brief_tool import BriefTool
from xuloom.tools.browser_tool import BrowserTool
from xuloom.tools.command_tool import CommandTool
from xuloom.tools.file_edit_tool import FileEditTool
from xuloom.tools.file_read_tool import FileReadTool
from xuloom.tools.file_write_tool import FileWriteTool
from xuloom.tools.glob_tool import GlobTool
from xuloom.tools.goal_tool import GoalReportTool
from xuloom.tools.grep_tool import GrepTool
from xuloom.tools.notebook_edit_tool import NotebookEditTool
from xuloom.tools.notebook_tool import NotebookTool
from xuloom.tools.plan_mode_tools import EnterPlanModeTool, ExitPlanModeTool
from xuloom.tools.plan_tool import PlanTool
from xuloom.tools.repl_tool import ReplTool
from xuloom.tools.sandbox_tool import SandboxTool
from xuloom.tools.schedule_tool import ScheduleTool
from xuloom.tools.send_message_tool import SendMessageTool
from xuloom.tools.task_tools import TaskCreateTool, TaskKillTool, TaskListTool, TaskReadTool, TaskStatusTool
from xuloom.tools.team_tools import TeamCreateTool, TeamDeleteTool
from xuloom.tools.todo_tool import TodoWriteTool
from xuloom.tools.tool_search_tool import ToolSearchTool
from xuloom.tools.web_tool import WebFetchTool, WebSearchTool

__all__ = ["default_tools", "LATENT_TOOL_NAMES"]

LATENT_TOOL_NAMES = frozenset({
    "tool_sequence",
    "goal_report_tool",
    "schedule_tool",
    "browser_tool",
    "notebook_tool",
    "notebook_edit_tool", "repl_tool", "sandbox_tool", "brief_tool",
    "task_create_tool", "task_status_tool", "task_read_tool", "task_kill_tool", "task_list_tool",
    "team_create_tool", "team_delete_tool", "send_message_tool",
})


def default_tools() -> dict:
    """Return new instances of every built-in tool, keyed by name."""
    tools = {
        "bash_tool": BashTool(),
        "command_tool": CommandTool(),
        "file_read_tool": FileReadTool(),
        "file_edit_tool": FileEditTool(),
        "file_write_tool": FileWriteTool(),
        "grep_tool": GrepTool(),
        "glob_tool": GlobTool(),
        "sandbox_tool": SandboxTool(),
        "task_create_tool": TaskCreateTool(),
        "task_status_tool": TaskStatusTool(),
        "task_read_tool": TaskReadTool(),
        "task_kill_tool": TaskKillTool(),
        "task_list_tool": TaskListTool(),
        "web_search_tool": WebSearchTool(),
        "web_fetch_tool": WebFetchTool(),
        "notebook_tool": NotebookTool(),
        "notebook_edit_tool": NotebookEditTool(),
        "plan_tool": PlanTool(),
        "todo_write_tool": TodoWriteTool(),
        "agent_tool": AgentTool(),
        "spawn_agent": SpawnAgentTool(),
        "browser_tool": BrowserTool(),
        "repl_tool": ReplTool(),
        "skill_tool": SkillTool(),
        "tool_search_tool": ToolSearchTool(),
        "goal_report_tool": GoalReportTool(),
        "schedule_tool": ScheduleTool(),
        "team_create_tool": TeamCreateTool(),
        "team_delete_tool": TeamDeleteTool(),
        "send_message_tool": SendMessageTool(),
        "ask_user_question_tool": AskUserQuestionTool(),
        "brief_tool": BriefTool(),
        "enter_plan_mode": EnterPlanModeTool(),
        "exit_plan_mode": ExitPlanModeTool(),
    }
    from xuloom.tools.tool_sequence import ToolSequenceTool
    from xuloom.agents.ultra_execution import UltraAnswerTool, UltraPrepareTool
    tools["tool_sequence"] = ToolSequenceTool()
    tools.update(ultra_answer=UltraAnswerTool(), ultra_prepare=UltraPrepareTool())
    return tools
