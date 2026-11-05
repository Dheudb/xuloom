from pydantic import BaseModel
from xuloom.tools.base import BaseAgenticTool


class EnterPlanModeInput(BaseModel):
    pass


class EnterPlanModeTool(BaseAgenticTool):
    name = "enter_plan_mode"
    description = (
        "Switch to plan mode. In plan mode, you can only use read-only tools "
        "and edit the plan file. Use this when you need to think through a complex "
        "task before making changes."
    )
    input_schema = EnterPlanModeInput
    risk_level = "low"
    is_read_only = True

    def prompt(self) -> str:
        return """Use this tool proactively when you're about to start a non-trivial implementation task. Getting user sign-off on your approach before writing code prevents wasted effort and ensures alignment. This tool transitions you into plan mode where you can explore the codebase and design an implementation approach for user approval.

## When to Use This Tool

**Prefer using enter_plan_mode** for implementation tasks unless they're simple. Use it when ANY of these conditions apply:

1. **New Feature Implementation**: Adding meaningful new functionality
2. **Multiple Valid Approaches**: The task can be solved in several different ways
3. **Code Modifications**: Changes that affect existing behavior or structure
4. **Architectural Decisions**: The task requires choosing between patterns or technologies
5. **Multi-File Changes**: The task will likely touch more than 2-3 files
6. **Unclear Requirements**: You need to explore before understanding the full scope
7. **User Preferences Matter**: The implementation could reasonably go multiple ways

## When NOT to Use This Tool
Only skip enter_plan_mode for simple tasks:
- Single-line or few-line fixes (typos, obvious bugs, small tweaks)
- Adding a single function with clear requirements
- Tasks where the user has given very specific, detailed instructions
- Pure research/exploration tasks (use agent_tool with explore type instead)

## Examples of When to Plan
- "Add authentication to the API" → Plan (architecture decision)
- "Refactor the database layer" → Plan (large scope)
- "Fix the typo on line 42" → Don't plan (trivial)
- "Add a comment to this function" → Don't plan (trivial)

## Important Notes
- This tool REQUIRES user approval — they must consent to entering plan mode
- If unsure whether to use it, err on the side of planning
- In plan mode, you can read files and launch explore agents in parallel

DO NOT write or edit any files except the plan file. Detailed workflow instructions will follow."""

    async def execute(self) -> str:
        plan_manager = self.context.get("plan_manager")
        if not plan_manager:
            return "Error: Plan Manager is unavailable."
        current_mode = plan_manager.get_mode()
        if current_mode == "plan":
            return "Already in plan mode."
        plan_manager.set_mode("plan")
        plan_file = plan_manager.plan_file_path
        return (
            f"Switched to plan mode. "
            f"You can now only use read-only tools and edit the plan file.\n\n"
            f"Plan file: {plan_file}\n"
            f"Use file_write_tool to create the plan file, or file_edit_tool to update it.\n"
            f"Use exit_plan_mode when your plan is ready for user approval."
        )


class ExitPlanModeInput(BaseModel):
    pass


class ExitPlanModeTool(BaseAgenticTool):
    name = "exit_plan_mode"
    description = (
        "Signal that your plan is complete and ready for user review. "
        "The user will see the contents of your plan file when they review it."
    )
    input_schema = ExitPlanModeInput
    risk_level = "low"
    is_read_only = True

    def prompt(self) -> str:
        return """Use this tool when you are in plan mode and have finished writing your plan to the plan file and are ready for user approval.

## How This Tool Works
- You should have already written your plan to the plan file specified in the plan mode system message
- This tool does NOT take the plan content as a parameter — it will read the plan from the file you wrote
- This tool simply signals that you're done planning and ready for the user to review and approve
- The user will see the contents of your plan file when they review it

## When to Use This Tool
IMPORTANT: Only use this tool when the task requires planning the implementation steps of a task that requires writing code. For research tasks where you're gathering information, searching files, reading files or in general trying to understand the codebase — do NOT use this tool.

## Before Using This Tool
Ensure your plan is complete and unambiguous:
- If you have unresolved questions about requirements or approach, use ask_user_question_tool first
- Once your plan is finalized, use THIS tool to request approval

**Important:** Do NOT use ask_user_question_tool to ask "Is this plan okay?" or "Should I proceed?" — that's exactly what THIS tool does. exit_plan_mode inherently requests user approval of your plan.

## Examples
1. "Search for and understand the implementation of vim mode" → Do NOT use exit_plan_mode (research, not planning)
2. "Help me implement yank mode for vim" → Use exit_plan_mode after planning implementation steps
3. "Add user authentication" → If unsure about auth method, use ask_user_question_tool first, then exit_plan_mode after clarifying"""

    async def execute(self) -> str:
        plan_manager = self.context.get("plan_manager")
        if not plan_manager:
            return "Error: Plan Manager is unavailable."
        current_mode = plan_manager.get_mode()
        if current_mode == "normal":
            return "Already in normal mode."

        plan_file = plan_manager.plan_file_path
        plan_content = plan_manager.get_plan()

        if not plan_content or not plan_content.strip():
            return (
                f"No plan file found at {plan_file}. "
                f"Please write your plan to this file before calling exit_plan_mode."
            )

        approval = self.context.get("plan_approval_callback")
        if not approval:
            return "Plan approval is required to leave plan mode, but no approval handler is configured. Stay in plan mode."
        if approval:
            import inspect
            answer = approval(plan_content)
            if inspect.isawaitable(answer):
                answer = await answer
            if answer != "allow":
                return "Plan was not approved. Stay in plan mode and revise it according to user feedback."

        plan_manager.set_mode("normal")

        return (
            "User has approved your plan. You can now start coding. "
            "Start with updating your todo list if applicable.\n\n"
            f"Plan file: {plan_file}\n\n"
            f"## Plan:\n{plan_content}"
        )
