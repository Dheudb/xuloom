"""
SendMessageTool for inter-agent communication within a team.

Routes messages to team members via in-process async queues managed by TeamManager.
The coordinator/lead can send follow-up instructions to running workers,
broadcast to all, or send structured control messages (shutdown, etc.).
"""

from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool


class SendMessageInput(BaseModel):
    to: str = Field(
        ...,
        description=(
            'Recipient name. Use a team member name (e.g. "researcher") for '
            'direct messages, or "*" to broadcast to all active members.'
        ),
    )
    message: str = Field(
        ...,
        description="The message content to send.",
    )
    msg_type: str = Field(
        "text",
        description=(
            'Message type. "text" for normal messages, '
            '"shutdown_request" to ask a worker to finish up.'
        ),
    )


class SendMessageTool(BaseAgenticTool):
    name = "send_message_tool"
    description = (
        "Send a message to a team member or broadcast to all members. "
        "Use this to give follow-up instructions to running worker agents, "
        "coordinate between workers, or request graceful shutdown. "
        "Requires an active team (created via team_create_tool)."
    )
    input_schema = SendMessageInput
    risk_level = "low"

    def prompt(self) -> str:
        team_mgr = self.context.get("team_manager")
        if not team_mgr or not team_mgr.is_active:
            return ""
        members = team_mgr.get_non_lead_members()
        if not members:
            return "No workers spawned yet. Use agent_tool to spawn workers first."
        names = ", ".join(f'"{m.name}"' for m in members if m.is_active)
        return (
            f"Active team members you can message: {names}. "
            "Use send_message_tool to send follow-up instructions."
        )

    async def execute(self, to: str, message: str, msg_type: str = "text") -> str:
        team_mgr = self.context.get("team_manager")
        if not team_mgr:
            return "Error: Team manager not available."
        if not team_mgr.is_active:
            return "Error: No active team. Create one with team_create_tool first."

        from_name = self.context.get("team_member_name", "team-lead")

        result = team_mgr.send_message(
            from_name=from_name,
            to_name=to,
            text=message,
            msg_type=msg_type,
        )

        if not result.get("ok"):
            return f"Error: {result.get('error', 'Message delivery failed.')}"

        if result.get("broadcast"):
            return f"Broadcast delivered to {result['delivered_to']} member(s)."
        return f'Message delivered to "{to}".'
