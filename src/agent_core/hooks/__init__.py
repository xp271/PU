"""
Hooks system — extensibility points for tool-use pipelines and turn-loop lifecycle.

Public API
----------
- ``HookRegistry`` — per-query hook store (priority-ordered).
- ``PreToolUseHook`` / ``PostToolUseHook`` / ``StopHook`` / ``SubagentStartHook`` — Protocols.
- ``HookResult`` / ``HookBlock`` / ``HookModify`` / ``HookContinue`` / ``StopResult`` — data models.
- ``run_pre_tool_use_hooks`` / ``run_post_tool_use_hooks`` / ``run_stop_hooks`` /
  ``run_subagent_start_hooks`` — pipeline executors.
"""

from __future__ import annotations

from .pipeline import (
    run_post_tool_use_hooks,
    run_pre_tool_use_hooks,
    run_stop_hooks,
    run_subagent_start_hooks,
)
from .registry import HookRegistry
from .types import (
    HookBlock,
    HookContinue,
    HookModify,
    HookResult,
    PreToolUseHook,
    PostToolUseHook,
    StopHook,
    StopResult,
    SubagentStartHook,
)

__all__ = [
    "HookRegistry",
    "PreToolUseHook",
    "PostToolUseHook",
    "StopHook",
    "SubagentStartHook",
    "HookBlock",
    "HookModify",
    "HookContinue",
    "HookResult",
    "StopResult",
    "run_pre_tool_use_hooks",
    "run_post_tool_use_hooks",
    "run_stop_hooks",
    "run_subagent_start_hooks",
]
