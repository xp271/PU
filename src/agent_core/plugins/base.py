"""
Plugin abstract base class.

A single Plugin can contribute tools, hooks, agent definitions, and skills.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar

from ..agents.definition import AgentDefinition
from ..tools.base import Tool


class Plugin(ABC):
    """Abstract base class for discoverable plugins.

    Subclasses that define a ``name`` ClassVar are automatically
    registered by ``PluginRegistry.discover()`` when listed in the
    ``agent_core.plugins`` entry-point group.

    Each lifecycle method is a coroutine so slow initialisation
    (e.g. downloading remote configuration) does not block the agent
    startup.
    """

    name: ClassVar[str]           # mandatory — unique plugin identifier
    version: ClassVar[str] = "0.1.0"

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def on_enable(self) -> None:
        """Called after the plugin's contributions have been injected."""

    async def on_disable(self) -> None:
        """Called before the plugin's contributions are removed."""

    # ------------------------------------------------------------------
    # Contributions (override to provide assets)
    # ------------------------------------------------------------------

    def get_tools(self) -> list[type[Tool]]:
        """Return ``Tool`` subclasses to register."""
        return []

    def get_hooks(self) -> list[Any]:
        """Return hook **instances** (must expose ``hook_kind`` attribute)."""
        return []

    def get_agent_definitions(self) -> list[AgentDefinition]:
        """Return ``AgentDefinition`` instances to register."""
        return []

    def get_skills(self) -> list[Any]:
        """Return ``SkillDefinition`` instances to register."""
        return []
