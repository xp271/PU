"""
Plugin lifecycle helpers — enable / disable with error isolation.

These are thin wrappers around ``PluginRegistry.enable()`` /
``PluginRegistry.disable()`` that catch exceptions from individual
plugins so one malfunctioning plugin never blocks the whole agent.
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import PluginRegistry

logger = logging.getLogger("agent_core.plugins")


async def safe_enable_plugins(
    registry: PluginRegistry,
    names: list[str],
    **kwargs: Any,
) -> None:
    """Enable each plugin in *names*, logging and skipping on error."""
    for name in names:
        try:
            await registry.enable(name, **kwargs)
        except Exception as exc:
            logger.warning(
                "Failed to enable plugin '%s': %s", name, exc
            )


async def safe_disable_all(
    registry: PluginRegistry,
    **kwargs: Any,
) -> None:
    """Disable every currently enabled plugin, logging errors."""
    for name in list(registry.enabled_plugins()):
        try:
            await registry.disable(name, **kwargs)
        except Exception as exc:
            logger.warning(
                "Failed to disable plugin '%s': %s", name, exc
            )
