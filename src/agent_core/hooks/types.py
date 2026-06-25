"""
Hook Protocol definitions and return-type models.

Hooks are typed via ``typing.Protocol`` for static checking, but the
**runtime** classification uses an explicit ``hook_kind`` ClassVar because
all four protocols share the same ``__call__`` shape and cannot be
distinguished via ``@runtime_checkable`` + ``isinstance``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# HookKind — explicit runtime discriminator
# ---------------------------------------------------------------------------

HookKind = Literal["pre_tool_use", "post_tool_use", "stop", "subagent_start"]


class HookBase:
    """Optional base for hook implementations — carries ``hook_kind``.

    Users may skip this base as long as the object exposes a
    ``hook_kind: HookKind`` attribute (duck-typing).
    """

    hook_kind: ClassVar[HookKind]


# ---------------------------------------------------------------------------
# ActionResult models (what each hook may return)
# ---------------------------------------------------------------------------


class HookBlock(BaseModel):
    """The hook demands the operation be blocked (error result)."""

    action: Literal["block"] = "block"
    reason: str = ""
    message: str | None = None  # optional message to inject into conversation


class HookModify(BaseModel):
    """The hook wants to replace the tool input or output."""

    action: Literal["modify"] = "modify"
    new_result: Any  # replacement content (ToolCallResult.content or tool_input dict)


class HookContinue(BaseModel):
    """The hook allows the operation to proceed unchanged."""

    action: Literal["continue"] = "continue"


HookResult = HookContinue | HookBlock | HookModify


# ---------------------------------------------------------------------------
# StopHook result (different shape from generic HookResult)
# ---------------------------------------------------------------------------


class StopResult(BaseModel):
    """Decision produced by a StopHook.

    When ``should_stop=True`` the turn loop terminates gracefully with
    ``DoneMessage("end_turn")``.
    """

    should_stop: bool = False
    reason: str = ""
    message: str | None = None  # injected as AssistantText before DoneMessage


# ---------------------------------------------------------------------------
# Hook Protocol definitions
# ---------------------------------------------------------------------------


class PreToolUseHook(Protocol):
    """Called **before** tool execution (after bash_guard + permission).

    Must expose ``hook_kind: ClassVar[HookKind]`` set to ``"pre_tool_use"``.

    Returns
    -------
    HookContinue
        Tool executes normally.
    HookBlock
        Tool is refused; an ``is_error=True`` ``ToolResult`` is returned.
    HookModify
        The hook replaced ``tool_input``.  The pipeline re-runs
        bash_guard + permission on the modified input before executing.
    """

    hook_kind: ClassVar[HookKind]

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,  # ToolUseContext (avoid circular import)
    ) -> HookResult: ...


class PostToolUseHook(Protocol):
    """Called **after** tool execution but **before** result persistence/truncation.

    Must expose ``hook_kind: ClassVar[HookKind]`` set to ``"post_tool_use"``.

    Receives the **full** ``ToolCallResult`` so ``HookModify`` operates on
    real content, not a truncated preview.

    Returns
    -------
    HookContinue
        Result is returned as-is.
    HookBlock
        Result is replaced with an ``is_error=True`` ToolResult.
    HookModify
        ``ToolCallResult.content`` is replaced with ``new_result``.
    """

    hook_kind: ClassVar[HookKind]

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,  # ToolCallResult (avoid circular import)
        context: Any,  # ToolUseContext
    ) -> HookResult: ...


class StopHook(Protocol):
    """Evaluated at the **top of each turn** (when messages are in a legal state).

    Must expose ``hook_kind: ClassVar[HookKind]`` set to ``"stop"``.

    For cost capping, goal detection, runaway protection, etc.
    The hook fires **after** notification drain and **before** budget/truncation
    checks so that the final DoneMessage is always part of a well-formed stream.

    Returns
    -------
    StopResult
        ``should_stop=True`` terminates the loop with ``DoneMessage("end_turn")``.
    """

    hook_kind: ClassVar[HookKind]

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        context: Any,  # ToolUseContext
    ) -> StopResult: ...


class SubagentStartHook(Protocol):
    """Called before a sub-agent is launched (inside ``AgentTool.call()``).

    Must expose ``hook_kind: ClassVar[HookKind]`` set to ``"subagent_start"``.

    Use for audit, rate-limiting, or logging.  ``HookBlock`` prevents the
    sub-agent from starting.  ``HookModify`` is ignored (treated as continue).
    """

    hook_kind: ClassVar[HookKind]

    async def __call__(
        self,
        agent_type: str,
        prompt: str,
        definition: Any,  # AgentDefinition
        context: Any,  # ToolUseContext
    ) -> HookResult: ...
