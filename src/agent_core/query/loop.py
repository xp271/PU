"""
Agent turn-loop core — ``query()`` async generator.

This is the heart of the Agent.  It orchestrates LLM calls, tool
execution, token-budget tracking, context truncation, and stop-condition
detection, yielding a stream of ``AgentMessage`` items consumed by
``Agent.run()`` / ``Agent.stream()``.

The loop is provider-agnostic — it only consumes ``InternalEvent``
and delegates provider details to ``BaseClient`` subclasses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from ..llm.base import get_client
from ..llm.events import InternalEvent, StopEvent, StreamStart, TokenCounts
from ..logging.tracer import TurnTracer
from ..models import (
    AgentConfig,
    AgentMessage,
    AssistantText,
    DoneMessage,
    ErrorMessage,
    ToolResult,
    ToolUse,
)
from ..tools.base import ToolUseContext
from ..tools.registry import get_tool
from .budget import estimate_tokens_from_messages, get_context_window, TokenBudget
from .context import enrich_context
from .truncate import hard_truncate

# Re-export for callers that only import from query
PermissionCallback = Any  # Callable[[str, dict[str, Any]], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def query(
    prompt: str,
    config: AgentConfig,
    trace_id: str,
    permission_callback: PermissionCallback | None = None,
    workspace_root: str | None = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Run the full agent turn loop for a single user prompt.

    Yields every ``AgentMessage`` (text, tool-use, tool-result, errors,
    terminal ``DoneMessage``) so that ``Agent.run()`` and
    ``Agent.stream()`` can both consume the same generator.
    """

    # ---- 1. Initialisation (runs once) -------------------------------------
    system = await enrich_context(workspace_root)

    messages: list[dict[str, Any]] = [
        _build_initial_user_message(prompt)
    ]

    tool_context = ToolUseContext(
        abort_signal=asyncio.Event(),
        read_file_state={},
        messages=messages,
        agent_id=trace_id,
        workspace_root=workspace_root,
    )

    model_id = config.model_id
    client = get_client(model_id, config)
    tools_schema = client.__class__.convert_tools(config.tools)

    budget = TokenBudget(
        max_turns=config.max_turns,
        context_window=get_context_window(model_id),
    )

    total_tokens = 0
    error_detail: str | None = None

    # ---- 2. Turn loop ------------------------------------------------------
    for turn_index in range(1, config.max_turns + 1):

        # -- 2a. Budget / truncation check before the LLM call --------------
        if budget.is_exhausted():
            yield DoneMessage(
                stop_reason="max_turns",
                total_tokens=total_tokens,
            )
            return

        estimated = estimate_tokens_from_messages(messages, system)
        if budget.needs_truncation(estimated):
            messages = hard_truncate(
                messages, max_tokens=budget.truncation_threshold
            )

        # -- 2b. LLM call ---------------------------------------------------
        try:
            tracer = TurnTracer(trace_id=trace_id)
            raw_stream = client.stream_messages(
                messages=messages,
                tools=tools_schema,
                system=system if system else None,
                max_tokens=4096,
                turn_index=turn_index,
            )
            wrapped = tracer.wrap(raw_stream)

            assistant_texts: list[str] = []
            tool_uses_in_turn: list[ToolUse] = []
            turn_stop_reason = "end_turn"

            async for event in wrapped:
                match event.type:
                    case "stream_start":
                        pass  # consumed by tracer; not exposed externally

                    case "assistant_text":
                        assistant_texts.append(event.content)  # type: ignore[attr-defined]
                        yield event  # transparent pass-through

                    case "tool_use":
                        tu = ToolUse(
                            tool_use_id=event.tool_use_id,  # type: ignore[attr-defined]
                            tool_name=event.tool_name,  # type: ignore[attr-defined]
                            tool_input=event.tool_input,  # type: ignore[attr-defined]
                        )
                        tool_uses_in_turn.append(tu)
                        yield tu

                    case "token_counts":
                        budget.record_turn(
                            turn_index,
                            input_tokens=event.input_tokens,  # type: ignore[attr-defined]
                            output_tokens=event.output_tokens,  # type: ignore[attr-defined]
                        )
                        total_tokens += (
                            event.input_tokens + event.output_tokens  # type: ignore[attr-defined]
                        )

                    case "stop":
                        normalized, detail = client.__class__.normalize_stop_reason(
                            event.stop_reason  # type: ignore[attr-defined]
                        )
                        turn_stop_reason = normalized
                        if detail:
                            error_detail = detail

                    case "error":
                        yield event  # ErrorMessage — pass through
                        yield DoneMessage(
                            stop_reason="error",
                            total_tokens=total_tokens,
                            error_detail=event.message,  # type: ignore[attr-defined]
                        )
                        return

        except Exception as exc:
            yield ErrorMessage(message=str(exc), error_type=type(exc).__name__)
            yield DoneMessage(
                stop_reason="error",
                total_tokens=total_tokens,
                error_detail=str(exc),
            )
            return

        # -- 2c. Build & append assistant message ---------------------------
        messages.append(
            _build_assistant_message(assistant_texts, tool_uses_in_turn)
        )

        # -- 2d. No tool uses → terminal turn -------------------------------
        if not tool_uses_in_turn:
            stop_reason = turn_stop_reason
            if stop_reason == "permission_denied":
                pass  # preserve
            yield DoneMessage(
                stop_reason=stop_reason,  # type: ignore[arg-type]
                total_tokens=total_tokens,
                error_detail=error_detail,
            )
            return

        # -- 2e. Execute tools & yield results ------------------------------
        tool_results = await _execute_tools_sequential(
            tool_uses_in_turn, tool_context, permission_callback
        )
        for tr in tool_results:
            yield tr

        # Build tool-result user message & append
        messages.append(_build_tool_result_user_message(tool_results))

        # -- 2f. Diminishing-returns check (reset on tool-use turns) --------
        budget.reset_diminishing_returns()
        # (diminishing returns only checked on turns WITHOUT tool use,
        #  and we've already handled the no-tool-use case above.)

    # ---- 3. Loop exhausted (max_turns reached) -----------------------------
    yield DoneMessage(
        stop_reason="max_turns",
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Message builders (Anthropic content-block format)
# ---------------------------------------------------------------------------


def _build_initial_user_message(prompt: str) -> dict[str, Any]:
    """Create the first ``role: "user"`` message from the raw prompt."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": prompt}],
    }


def _build_assistant_message(
    texts: list[str],
    tool_uses: list[ToolUse],
) -> dict[str, Any]:
    """Build an assistant message from accumulated text and tool-use blocks.

    Empty text fragments are skipped so that no ``{"type":"text","text":""}``
    blocks are produced.
    """
    content: list[dict[str, Any]] = []

    for t in texts:
        stripped = t.strip()
        if stripped:
            content.append({"type": "text", "text": stripped})

    for tu in tool_uses:
        content.append({
            "type": "tool_use",
            "id": tu.tool_use_id,
            "name": tu.tool_name,
            "input": tu.tool_input,
        })

    return {"role": "assistant", "content": content}


def _build_tool_result_user_message(
    tool_results: list[ToolResult],
) -> dict[str, Any]:
    """Build a ``role: "user"`` message carrying tool-result blocks."""
    content: list[dict[str, Any]] = []
    for tr in tool_results:
        content.append({
            "type": "tool_result",
            "tool_use_id": tr.tool_use_id,
            "content": tr.content,
            "is_error": tr.is_error,
        })
    return {"role": "user", "content": content}


# ---------------------------------------------------------------------------
# Tool execution (minimal — replaced by full pipeline in Phase 2 / item 5)
# ---------------------------------------------------------------------------


async def _execute_tools_sequential(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None,
) -> list[ToolResult]:
    """Execute tool-use requests one at a time, in order.

    This is a placeholder for the full tool-execution pipeline (Phase 2
    item 5).  The replacement will add concurrency grouping, hook
    pipelines, result persistence, and bash AST safety checks — but its
    signature must remain compatible:
    ``(list[ToolUse], ToolUseContext, callback) -> list[ToolResult]``.
    """
    results: list[ToolResult] = []

    for tu in tool_uses:
        # 1. Resolve
        try:
            tool = get_tool(tu.tool_name)
        except KeyError:
            results.append(
                ToolResult(
                    tool_use_id=tu.tool_use_id,
                    tool_name=tu.tool_name,
                    content=f"Unknown tool: {tu.tool_name}",
                    is_error=True,
                )
            )
            continue

        # 2. Validate input
        try:
            validated_input = tool.validate_input(tu.tool_input)
        except Exception as exc:
            results.append(
                ToolResult(
                    tool_use_id=tu.tool_use_id,
                    tool_name=tu.tool_name,
                    content=f"Invalid input: {exc}",
                    is_error=True,
                )
            )
            continue

        # 3. Permission check
        if permission_callback is not None:
            try:
                allowed = await permission_callback(tu.tool_name, tu.tool_input)
                if not allowed:
                    results.append(
                        ToolResult(
                            tool_use_id=tu.tool_use_id,
                            tool_name=tu.tool_name,
                            content="Permission denied",
                            is_error=True,
                        )
                    )
                    continue
            except Exception as exc:
                results.append(
                    ToolResult(
                        tool_use_id=tu.tool_use_id,
                        tool_name=tu.tool_name,
                        content=f"Permission callback error: {exc}",
                        is_error=True,
                    )
                )
                continue

        # 4. Execute
        try:
            result = await tool.call(validated_input, context)
            results.append(
                ToolResult(
                    tool_use_id=tu.tool_use_id,
                    tool_name=tu.tool_name,
                    content=result.content,
                    is_error=result.is_error,
                )
            )
        except Exception as exc:
            results.append(
                ToolResult(
                    tool_use_id=tu.tool_use_id,
                    tool_name=tu.tool_name,
                    content=f"Tool execution error: {exc}",
                    is_error=True,
                )
            )

    return results
