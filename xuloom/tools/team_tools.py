"""
TeamCreateTool and TeamDeleteTool for multi-agent swarm coordination.
"""

from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool


class TeamCreateInput(BaseModel):
    team_name: str = Field(..., description="Name for the new team.")
    description: str = Field("", description="Optional team description/purpose.")


class TeamCreateTool(BaseAgenticTool):
    name = "team_create_tool"
    description = (
        "Create a new multi-agent team. This establishes a coordination group "
        "where you act as team lead and can spawn worker agents via agent_tool, "
        "then exchange messages with them via send_message_tool."
    )
    input_schema = TeamCreateInput
    risk_level = "medium"

    def prompt(self) -> str:
        return (
            "Use team_create_tool to create a named team before spawning coordinated workers. "
            "Once a team exists, workers spawned via agent_tool are automatically registered "
            "as team members. Use send_message_tool to send follow-up instructions or "
            "coordinate between workers. Use team_delete_tool when done."
        )

    async def execute(self, team_name: str, description: str = "") -> str:
        team_mgr = self.context.get("team_manager")
        if not team_mgr:
            return "Error: Team manager not available."

        engine_agent_id = self.context.get("agent_id", "")
        engine_model = self.context.get("primary_model", "")

        result = team_mgr.create_team(
            team_name=team_name,
            lead_agent_id=engine_agent_id,
            description=description,
            lead_model=engine_model,
        )

        if not result.get("ok"):
            return f"Error: {result.get('error', 'Unknown error')}"

        return (
            f"Team \"{team_name}\" created successfully.\n"
            f"Lead agent: {result['lead_agent_id'][:8]}...\n"
            "You are the team lead. Spawn workers with agent_tool and "
            "coordinate via send_message_tool."
        )


class TeamDeleteInput(BaseModel):
    pass


class TeamDeleteTool(BaseAgenticTool):
    name = "team_delete_tool"
    description = (
        "Delete the current team and clean up. All team members must be "
        "inactive before deletion."
    )
    input_schema = TeamDeleteInput
    risk_level = "low"

    async def execute(self) -> str:
        team_mgr = self.context.get("team_manager")
        if not team_mgr:
            return "Error: Team manager not available."

        result = team_mgr.delete_team()

        if not result.get("ok"):
            return f"Error: {result.get('error', 'Unknown error')}"

        return result.get("message", "Team deleted.")
