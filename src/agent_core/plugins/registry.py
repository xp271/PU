"""
PluginRegistry — discover and manage plugins.

Supports automatic discovery via ``importlib.metadata.entry_points``
(group ``agent_core.plugins``) and manual ``register()``.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

from .base import Plugin

logger = logging.getLogger("agent_core.plugins")


class PluginRegistry:
    """Registry of available plugins with enable/disable tracking.

    Does **not** hold references to tool/hook/skill registries — the
    ``enable()`` caller passes them in explicitly so that per-query
    instances receive plugin contributions.
    """

    GROUP_NAME = "agent_core.plugins"

    def __init__(self) -> None:
        # plugin *classes* (lazy-loaded) keyed by name
        self._available: dict[str, type[Plugin]] = {}
        # instances of enabled plugins
        self._enabled: dict[str, Plugin] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[str]:
        """Scan ``entry_points(group="agent_core.plugins")`` for plugins.

        Returns the list of newly discovered plugin names (not yet enabled).
        Already registered plugins are not re-registered.
        """
        new_names: list[str] = []
        try:
            entries = importlib.metadata.entry_points(
                group=self.GROUP_NAME
            )
        except TypeError:
            # Python 3.12+ — entry_points() expects keyword
            entries = importlib.metadata.entry_points().get(
                self.GROUP_NAME, []
            )

        for ep in entries:
            if ep.name in self._available:
                continue
            try:
                plugin_cls = ep.load()
            except Exception as exc:
                logger.warning(
                    "Failed to load plugin '%s' from entry point: %s",
                    ep.name,
                    exc,
                )
                continue
            if not issubclass(plugin_cls, Plugin):
                logger.warning(
                    "Entry point '%s' does not subclass Plugin — skipping",
                    ep.name,
                )
                continue
            if not getattr(plugin_cls, "name", None):
                logger.warning(
                    "Plugin '%s' has no 'name' ClassVar — skipping",
                    ep.name,
                )
                continue
            name = plugin_cls.name
            self._available[name] = plugin_cls
            new_names.append(name)

        return new_names

    # ------------------------------------------------------------------
    # Manual registration / unregistration
    # ------------------------------------------------------------------

    def register(self, plugin_cls: type[Plugin]) -> None:
        """Manually register a plugin class."""
        if not getattr(plugin_cls, "name", None):
            raise ValueError(f"Plugin {plugin_cls} must define a 'name' ClassVar")
        self._available[plugin_cls.name] = plugin_cls

    def unregister(self, name: str) -> None:
        """Remove a plugin from the available set (disable first if needed)."""
        if name in self._enabled:
            raise RuntimeError(
                f"Plugin '{name}' is currently enabled. Call disable() first."
            )
        self._available.pop(name, None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def enable(
        self,
        name: str,
        *,
        tool_registry: Any = None,
        hook_registry: Any = None,
        skill_registry: Any = None,
        agent_registry: Any = None,
    ) -> None:
        """Instantiate *name* and inject its contributions into the
        supplied registries.

        Registries are passed explicitly so per-query (hook/skill) instances
        receive plugin contributions rather than global singletons.
        """
        if name in self._enabled:
            return  # idempotent

        plugin_cls = self._available.get(name)
        if plugin_cls is None:
            raise KeyError(f"Unknown plugin: '{name}'")

        plugin = plugin_cls()
        self._enabled[name] = plugin

        # Inject contributions
        for tool_cls in plugin.get_tools():
            if tool_registry is not None:
                tool_registry.register(tool_cls)

        for hook in plugin.get_hooks():
            if hook_registry is not None:
                hook_registry.register(hook)

        for adef in plugin.get_agent_definitions():
            if agent_registry is not None:
                from ..agents.registry import register_agent
                register_agent(adef, source="plugin")

        for skill in plugin.get_skills():
            if skill_registry is not None:
                skill_registry.register(skill)

        await plugin.on_enable()

    async def disable(
        self,
        name: str,
        *,
        tool_registry: Any = None,
        hook_registry: Any = None,
        skill_registry: Any = None,
        agent_registry: Any = None,
    ) -> None:
        """Call ``on_disable()`` and remove contributions.

        Full removal of individual tool/hook/skill registrations is
        best-effort (tool registration is append-only; removal requires
        a fresh registry to be effective).
        """
        plugin = self._enabled.pop(name, None)
        if plugin is None:
            return

        await plugin.on_disable()

        # Best-effort removal — tool/agent registries are append-only,
        # but we can at least unregister skill entries.
        if skill_registry is not None:
            for skill in plugin.get_skills():
                try:
                    skill_registry.unregister(skill.name)
                except KeyError:
                    pass

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_enabled(self, name: str) -> bool:
        return name in self._enabled

    def enabled_plugins(self) -> list[str]:
        return list(self._enabled.keys())

    def available_plugins(self) -> list[str]:
        return list(self._available.keys())

    def get(self, name: str) -> Plugin | None:
        return self._enabled.get(name)
