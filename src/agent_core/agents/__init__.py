"""Agent definition system — data models, registry, loaders, and built-in agents.

Importing this package triggers registration of the three built-in agents
(GeneralPurpose, Explore, Plan) into the default global registry.
"""

from . import builtin  # noqa: F401 — side-effect: register built-in agents
from .definition import AgentDefinition, AgentMemory
from .loader import discover_agents, load_json_definition, load_markdown_definition
from .registry import AgentRegistry, get_agent, list_agents, register_agent

__all__ = [
    "AgentDefinition",
    "AgentMemory",
    "AgentRegistry",
    "discover_agents",
    "get_agent",
    "list_agents",
    "load_json_definition",
    "load_markdown_definition",
    "register_agent",
]
