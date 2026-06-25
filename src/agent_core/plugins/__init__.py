"""
Plugin system — discoverable, hot-pluggable extension modules.

Public API
----------
- ``Plugin`` — abstract base class.
- ``PluginRegistry`` — entry-point discovery + manual registration.
- ``safe_enable_plugins`` / ``safe_disable_all`` — lifecycle helpers.
"""

from __future__ import annotations

from .base import Plugin
from .lifecycle import safe_disable_all, safe_enable_plugins
from .registry import PluginRegistry

__all__ = [
    "Plugin",
    "PluginRegistry",
    "safe_enable_plugins",
    "safe_disable_all",
]
