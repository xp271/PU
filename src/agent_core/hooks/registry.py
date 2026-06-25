"""
HookRegistry — per-query hook store with priority ordering.

Every ``query()`` invocation gets its own ``HookRegistry`` instance (hung
on ``ToolUseContext.hook_registry``).  The module-level ``_default_hook_registry``
is a **read-only template** holding built-in hooks; per-query instances are
created via ``copy_with_overrides()``.
"""

from __future__ import annotations

import itertools
from typing import Any

# ---------------------------------------------------------------------------
# HookRegistry
# ---------------------------------------------------------------------------


class HookRegistry:
    """Per-query hook registry, sorted by priority.

    Hooks are routed to internal buckets based on their ``hook_kind``
    attribute, **not** via ``isinstance`` (all hooks share ``__call__``).
    """

    def __init__(self) -> None:
        # Each entry: (priority, monotonic_seq, hook_instance)
        self._seq = itertools.count()
        self._pre_tool_use: list[tuple[int, int, Any]] = []
        self._post_tool_use: list[tuple[int, int, Any]] = []
        self._stop: list[tuple[int, int, Any]] = []
        self._subagent_start: list[tuple[int, int, Any]] = []

        self._buckets: dict[str, list[tuple[int, int, Any]]] = {
            "pre_tool_use": self._pre_tool_use,
            "post_tool_use": self._post_tool_use,
            "stop": self._stop,
            "subagent_start": self._subagent_start,
        }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, hook: Any, priority: int = 100) -> None:
        """Register *hook*, routed to the correct bucket by ``hook.hook_kind``.

        Raises ``ValueError`` if *hook* lacks a valid ``hook_kind``.
        """
        kind = getattr(hook, "hook_kind", None)
        if kind not in self._buckets:
            raise ValueError(
                f"Hook {hook!r} is missing a valid hook_kind; "
                f"expected one of: pre_tool_use, post_tool_use, stop, subagent_start"
            )
        self._buckets[kind].append((priority, next(self._seq), hook))

    def register_all(self, hooks: list[Any], priority: int = 100) -> None:
        """Convenience: register a list of hooks at the same priority."""
        for hook in hooks:
            self.register(hook, priority)

    # ------------------------------------------------------------------
    # Queries (sorted: low priority first, then registration order)
    # ------------------------------------------------------------------

    def pre_tool_use_hooks(self) -> list[Any]:
        return [h for _, _, h in sorted(self._pre_tool_use)]

    def post_tool_use_hooks(self) -> list[Any]:
        return [h for _, _, h in sorted(self._post_tool_use)]

    def stop_hooks(self) -> list[Any]:
        return [h for _, _, h in sorted(self._stop)]

    def subagent_start_hooks(self) -> list[Any]:
        return [h for _, _, h in sorted(self._subagent_start)]

    # ------------------------------------------------------------------
    # Short-circuit
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        """Return ``True`` when no hooks are registered (pipeline can skip)."""
        return not (
            self._pre_tool_use
            or self._post_tool_use
            or self._stop
            or self._subagent_start
        )

    # ------------------------------------------------------------------
    # Isolation — create a child copy with extra hooks appended
    # ------------------------------------------------------------------

    def copy_with_overrides(self, extra_hooks: list[Any]) -> "HookRegistry":
        """Return a **new** ``HookRegistry`` that contains all hooks from
        *self* plus *extra_hooks* (registered at priority 100).

        Used when constructing a child agent's hook environment.
        """
        child = HookRegistry()
        for bucket in self._buckets.values():
            for pri, _seq, hook in bucket:
                child.register(hook, priority=pri)
        child.register_all(extra_hooks, priority=100)
        return child


# ---------------------------------------------------------------------------
# Module-level read-only template
#
# Built-in hooks (future: e.g. Bash safety audit) are registered here at
# import time.  ``query()`` creates per-query instances via
# ``_default_hook_registry.copy_with_overrides(config.hooks)``.
# Do **not** mutate this instance after startup.
# ---------------------------------------------------------------------------

_default_hook_registry = HookRegistry()
