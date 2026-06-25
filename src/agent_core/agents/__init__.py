"""Agent definition system — data models, registry, loaders, built-in agents,
and the sub-agent execution engine (Item 8).

Importing this package triggers registration of the three built-in agents
(GeneralPurpose, Explore, Plan) into the default global registry.
"""

from . import builtin  # noqa: F401 — side-effect: register built-in agents
from .definition import AgentDefinition, AgentMemory
from .fork import (
    filter_incomplete_tool_calls,
    fork_messages,
    resume_agent,
)
from .lifecycle import (
    AgentLifecycleState,
    enqueue_agent_notification,
    finalize_agent_tool,
    run_async_lifecycle,
)
from .loader import discover_agents, load_json_definition, load_markdown_definition
from .registry import AgentRegistry, get_agent, list_agents, register_agent
from .resolve import (
    build_child_engine,
    build_child_system_prompt,
    disallow_agent_at_depth_limit,
    MAX_AGENT_DEPTH,
    merge_mcp_servers,
    resolve_agent_tools,
    resolve_model,
)
from .run import run_agent
from .tool import AgentInput, AgentTool  # noqa: F401 — registered as tool via decorator

__all__ = [
    # Item 7 — data model + registry
    "AgentDefinition",
    "AgentMemory",
    "AgentRegistry",
    "discover_agents",
    "get_agent",
    "list_agents",
    "load_json_definition",
    "load_markdown_definition",
    "register_agent",
    # Item 7.6 — consumption contract
    "build_child_engine",
    "build_child_system_prompt",
    "disallow_agent_at_depth_limit",
    "MAX_AGENT_DEPTH",
    "merge_mcp_servers",
    "resolve_agent_tools",
    "resolve_model",
    # Item 8 — execution engine
    "run_agent",
    "AgentTool",
    "AgentInput",
    # Item 8 — lifecycle
    "AgentLifecycleState",
    "enqueue_agent_notification",
    "finalize_agent_tool",
    "run_async_lifecycle",
    # Item 8 — fork / resume
    "filter_incomplete_tool_calls",
    "fork_messages",
    "resume_agent",
]
