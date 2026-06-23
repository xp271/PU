"""Explore — read-only codebase exploration agent."""

from ..definition import AgentDefinition, AgentMemory
from ..registry import register_agent

EXPLORE = AgentDefinition(
    agent_type="Explore",
    when_to_use=(
        "Subagent used whenever the task requires searching across multiple "
        "files, directories, or patterns, or when the scope of code exploration "
        "is too large for a single read_file or search_file call. "
        "This agent should be invoked any time the task requires understanding "
        "or analyzing code spread across more than 2-3 files.\n"
        "The agent returns findings as text — it should NOT write code."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt=(
        "You are a code exploration specialist. Your job is to:\n"
        "1. Search the codebase efficiently using Glob and Grep\n"
        "2. Read and analyze relevant files thoroughly\n"
        "3. Report findings succinctly\n\n"
        "Important: Do NOT edit or write files. You are read-only."
    ),
    permission_mode=None,  # inherit parent; read-only auto-approve via engine step 6
    memory=AgentMemory.LOCAL,
)

register_agent(EXPLORE, source="builtin")
