from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Tool

_registry: dict[str, type[Tool]] = {}
_instances: dict[str, Tool] = {}


def register_tool(cls: type[Tool]) -> type[Tool]:
    name = getattr(cls, "name", None)
    if not name or not isinstance(name, str):
        raise TypeError(f"Tool class {cls.__name__} must define a 'name' ClassVar[str]")
    _registry[name] = cls
    _instances.pop(name, None)  # drop any stale cached instance on re-register
    return cls


def get_tool(name: str) -> Tool:
    cls = _registry.get(name)
    if cls is None:
        raise KeyError(f"Tool '{name}' is not registered. Available: {sorted(_registry)}")
    inst = _instances.get(name)
    if inst is None:
        inst = cls()
        _instances[name] = inst
    return inst


def registered_tools() -> dict[str, type[Tool]]:
    return dict(_registry)
