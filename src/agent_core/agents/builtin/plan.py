"""Plan — read-only planning agent (produces plans, never writes code)."""

from ..definition import AgentDefinition, AgentMemory
from ..registry import register_agent

PLAN = AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Subagent used before writing any code. This agent should be invoked "
        "when the user gives a complex task that requires planning or when "
        "the approach isn't immediately obvious. "
        "The agent reads relevant code, designs an architecture, and returns "
        "a written plan — it should NOT write implementation code."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt=(
        "You are a planning specialist. Your job is to:\n"
        "1. Understand the user's requirements\n"
        "2. Explore the relevant codebase to understand existing code\n"
        "3. Design an implementation approach\n"
        "4. Produce a clear, detailed plan\n\n"
        "Important: Do NOT write implementation code. Produce only a plan."
    ),
    permission_mode=None,  # inherit parent; read-only auto-approve via engine step 6
    memory=AgentMemory.LOCAL,
)

register_agent(PLAN, source="builtin")
