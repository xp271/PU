"""GeneralPurpose — the default agent (all tools, inherits parent permissions)."""

from ..definition import AgentDefinition, AgentMemory
from ..registry import register_agent

GENERAL_PURPOSE = AgentDefinition(
    agent_type="GeneralPurpose",
    when_to_use=(
        "General-purpose agent for any task that doesn't require a specialist."
    ),
    tools=["*"],
    system_prompt="You are a general-purpose assistant.",
    permission_mode=None,  # inherit from parent
    memory=AgentMemory.NONE,
)

register_agent(GENERAL_PURPOSE, source="builtin")
