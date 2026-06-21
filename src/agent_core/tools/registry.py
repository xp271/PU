"""
Tool registry — instance-based with module-level default for backward compat.

The ``ToolRegistry`` class is the canonical registry.  Module-level
convenience functions (``register_tool``, ``get_tool``, ``registered_tools``)
delegate to a shared *default* instance so existing code continues to work
without changes.

For query-scoped isolation (MCP tools, sub-agents), create a fresh
``ToolRegistry()`` instance and pass it through the call chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Tool


class ToolRegistry:
    """Per-scope tool registry (one per ``query()`` / sub-agent).

    Supports the same API as the previous module-global dict plus
    the ``unregister`` / ``contains`` / ``names`` methods needed by
    the MCP bridge and sub-agent isolation.
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[Tool]] = {}
        self._instances: dict[str, Tool] = {}

    # -- mutation -------------------------------------------------------

    def register(self, cls: type[Tool]) -> type[Tool]:
        name = getattr(cls, "name", None)
        if not name or not isinstance(name, str):
            raise TypeError(
                f"Tool class {cls.__name__} must define a 'name' ClassVar[str]"
            )
        self._registry[name] = cls
        self._instances.pop(name, None)  # drop stale cached instance
        return cls

    def unregister(self, name: str) -> None:
        self._registry.pop(name, None)
        self._instances.pop(name, None)

    # -- query ----------------------------------------------------------

    def get(self, name: str) -> Tool:
        cls = self._registry.get(name)
        if cls is None:
            raise KeyError(
                f"Tool '{name}' is not registered. "
                f"Available: {sorted(self._registry)}"
            )
        inst = self._instances.get(name)
        if inst is None:
            inst = cls()
            self._instances[name] = inst
        return inst

    def contains(self, name: str) -> bool:
        return name in self._registry

    def names(self) -> list[str]:
        return list(self._registry)

    def registered_tools(self) -> dict[str, type[Tool]]:
        return dict(self._registry)


# ------------------------------------------------------------------
# Module-level default (backward-compatible global)
# ------------------------------------------------------------------

_default_registry = ToolRegistry()


def register_tool(cls: type[Tool]) -> type[Tool]:
    """Decorator — registers *cls* in the default global registry."""
    return _default_registry.register(cls)


def get_tool(name: str) -> Tool:
    """Look up a cached singleton tool instance from the default registry."""
    return _default_registry.get(name)


def registered_tools() -> dict[str, type[Tool]]:
    """Return a snapshot of the default registry's tool classes."""
    return _default_registry.registered_tools()
