"""
Hook pipeline executors.

Each function runs the relevant slice of a ``HookRegistry``, respecting
priority ordering and short-circuit semantics.
"""

from __future__ import annotations

from typing import Any

from .registry import HookRegistry
from .types import HookBlock, HookContinue, HookModify, StopResult


# ---------------------------------------------------------------------------
# Pre-tool-use
# ---------------------------------------------------------------------------


async def run_pre_tool_use_hooks(
    tool_name: str,
    tool_input: dict[str, Any],
    context: Any,  # ToolUseContext
    registry: HookRegistry | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the PreToolUse hook chain.

    Returns
    -------
    (modified_input, block_reason)
        ``modified_input``
            ``None`` — use the original input unchanged.
            ``dict`` — one or more hooks returned ``HookModify``;
            the **last** modification wins.
        ``block_reason``
            ``None`` — operation may proceed.
            ``str`` — a hook returned ``HookBlock`` with this reason.
    """
    if registry is None or registry.is_empty():
        return None, None

    effective_input: dict[str, Any] | None = None
    hooks = registry.pre_tool_use_hooks()
    if not hooks:
        return None, None

    for hook in hooks:
        try:
            result = await hook(tool_name, tool_input, context)
        except Exception as exc:
            # A misbehaving hook blocks the operation rather than crashing
            # the entire turn loop.
            return None, f"PreToolUse hook {hook!r} raised: {exc}"

        if isinstance(result, HookBlock):
            return None, result.reason or "blocked by pre-use hook"
        elif isinstance(result, HookModify):
            effective_input = result.new_result
        # HookContinue — no-op

    return effective_input, None


# ---------------------------------------------------------------------------
# Post-tool-use
# ---------------------------------------------------------------------------


async def run_post_tool_use_hooks(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: Any,  # ToolCallResult
    context: Any,  # ToolUseContext
    registry: HookRegistry | None = None,
) -> Any:  # ToolCallResult (may be modified)
    """Run the PostToolUse hook chain.

    Returns the (possibly modified) ``ToolCallResult``.
    """
    if registry is None or registry.is_empty():
        return tool_result

    hooks = registry.post_tool_use_hooks()
    if not hooks:
        return tool_result

    current_result = tool_result
    for hook in hooks:
        try:
            result = await hook(tool_name, tool_input, current_result, context)
        except Exception as exc:
            current_result.is_error = True
            current_result.content = f"PostToolUse hook {hook!r} raised: {exc}"
            return current_result

        if isinstance(result, HookBlock):
            current_result.is_error = True
            current_result.content = result.reason or "blocked by post-use hook"
            return current_result
        elif isinstance(result, HookModify):
            current_result.content = result.new_result
        # HookContinue — no-op

    return current_result


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def run_stop_hooks(
    messages: list[dict[str, Any]],
    context: Any,  # ToolUseContext
    registry: HookRegistry | None = None,
) -> StopResult:
    """Run the Stop hook chain.

    The first hook that returns ``should_stop=True`` short-circuits.
    """
    if registry is None or registry.is_empty():
        return StopResult()

    hooks = registry.stop_hooks()
    if not hooks:
        return StopResult()

    for hook in hooks:
        try:
            result = await hook(messages, context)
        except Exception:
            # A malfunctioning stop hook should not crash the loop;
            # treat as "no opinion".
            continue

        if isinstance(result, StopResult) and result.should_stop:
            return result

    return StopResult()


# ---------------------------------------------------------------------------
# Sub-agent start
# ---------------------------------------------------------------------------


async def run_subagent_start_hooks(
    agent_type: str,
    prompt: str,
    definition: Any,  # AgentDefinition
    context: Any,  # ToolUseContext
    registry: HookRegistry | None = None,
) -> tuple[bool, str | None]:
    """Run the SubagentStart hook chain.

    Returns
    -------
    (blocked, reason)
        ``blocked=True`` — sub-agent launch is denied.
    """
    if registry is None or registry.is_empty():
        return False, None

    hooks = registry.subagent_start_hooks()
    if not hooks:
        return False, None

    for hook in hooks:
        try:
            result = await hook(agent_type, prompt, definition, context)
        except Exception as exc:
            return True, f"SubagentStart hook {hook!r} raised: {exc}"

        if isinstance(result, HookBlock):
            return True, result.reason or "blocked by subagent-start hook"
        # HookModify / HookContinue — no-op for this hook kind

    return False, None
