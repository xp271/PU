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
import logging
import os
import tempfile
import time as _time
from collections.abc import AsyncGenerator
from typing import Any

logger = logging.getLogger("agent_core.query")

import aiofiles
import aiofiles.os

from ..llm.base import get_client
from ..logging.tracer import TurnTracer
from ..models import (
    AgentConfig,
    AgentMessage,
    AssistantText,
    CompactMessage,
    DoneMessage,
    ErrorMessage,
    ToolResult,
    ToolUse,
)
from ..permissions.bash_guard import analyze_bash_command
from ..permissions.engine import PermissionEngine
from ..tools.base import ToolCallResult, ToolUseContext
from ..tools.registry import get_tool
from .budget import estimate_tokens_from_messages, get_context_window, TokenBudget
from .context import enrich_context
from .truncate import hard_truncate
from ..compression.trigger import can_compress
from ..compression.summarizer import compress, CompressionError

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

    permission_engine = PermissionEngine.from_config(
        permission_mode=config.permission_mode,
    )

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
            # -- Compression preferred; truncation as fallback ---------------
            if can_compress(messages, config.compression):
                from ..compression.trigger import _partition_rounds

                rounds = _partition_rounds(messages)
                round_count = len(rounds)
                compressed_rounds = round_count - config.compression.keep_recent_rounds

                yield CompactMessage(
                    status="started",
                    rounds_compressed=compressed_rounds,
                    rounds_preserved=config.compression.keep_recent_rounds,
                    tokens_before=estimated,
                )

                try:
                    messages, summary, attempts = await compress(
                        client=client,
                        messages=messages,
                        system=system if system else None,
                        model_id=model_id,
                        config=config.compression,
                    )

                    new_estimated = estimate_tokens_from_messages(messages, system)
                    ratio = new_estimated / estimated if estimated > 0 else 0

                    # Accumulate compression token costs (not counted as turns)
                    for attempt in attempts:
                        budget.total_input_tokens += attempt.input_tokens_consumed
                        budget.total_output_tokens += attempt.output_tokens_consumed
                        total_tokens += attempt.tokens_consumed

                    yield CompactMessage(
                        status="completed",
                        rounds_compressed=compressed_rounds,
                        rounds_preserved=config.compression.keep_recent_rounds,
                        tokens_before=estimated,
                        tokens_after=new_estimated,
                        compression_ratio=ratio,
                        summary=summary,
                        retries_used=len(attempts) - 1,
                    )

                    estimated = new_estimated
                    tool_context.messages = messages

                    # Compression succeeded but might still be over threshold
                    if budget.needs_truncation(estimated):
                        messages = hard_truncate(
                            messages, max_tokens=budget.truncation_threshold
                        )
                        tool_context.messages = messages

                except CompressionError as exc:
                    logger.warning(
                        "Compression failed (retries exhausted), "
                        "falling back to hard truncation: %s",
                        exc,
                    )
                    yield CompactMessage(
                        status="skipped",
                        tokens_before=estimated,
                        error_detail=str(exc),
                    )
                    messages = hard_truncate(
                        messages, max_tokens=budget.truncation_threshold
                    )
                    tool_context.messages = messages

            else:
                # Compression unavailable — direct truncation
                messages = hard_truncate(
                    messages, max_tokens=budget.truncation_threshold
                )
                tool_context.messages = messages

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
        tool_results = await run_tools(
            tool_uses_in_turn,
            tool_context,
            permission_callback=permission_callback,
            permission_engine=permission_engine,
            max_concurrency=config.max_concurrency,
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
# Tool execution pipeline (Phase 2 / item 5)
# ---------------------------------------------------------------------------

_RESULT_TRUNCATION_BYTES = 50 * 1024  # 50 KB
_RESULT_PREVIEW_CHARS = 1_500


async def run_tools(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    *,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
    max_concurrency: int = 5,
) -> list[ToolResult]:
    """Execute a batch of tool-use requests with concurrency orchestration.

    Non-concurrency-safe tools run serially; concurrency-safe tools run
    in parallel with an ``asyncio.Semaphore`` cap.  Results are returned
    in the same order as *tool_uses*.

    **Signature** — compatible replacement for the old
    ``_execute_tools_sequential``.
    """
    # Fast path: single tool — no concurrency overhead
    if len(tool_uses) == 1:
        tu = tool_uses[0]
        return [await run_tool_use(tu, context, permission_callback, permission_engine)]

    results: dict[int, ToolResult] = {}
    pending_batch: list[tuple[int, ToolUse]] = []

    for idx, tu in enumerate(tool_uses):
        is_concurrent = _tool_is_concurrency_safe(tu.tool_name)

        if is_concurrent:
            pending_batch.append((idx, tu))
        else:
            # Flush pending concurrent batch first
            if pending_batch:
                batch_results = await _run_concurrent_batch(
                    pending_batch, context, permission_callback,
                    permission_engine, max_concurrency,
                )
                results.update(batch_results)
                pending_batch.clear()
            # Execute serial tool
            results[idx] = await run_tool_use(
                tu, context, permission_callback, permission_engine,
            )

    # Flush remaining concurrent batch
    if pending_batch:
        batch_results = await _run_concurrent_batch(
            pending_batch, context, permission_callback,
            permission_engine, max_concurrency,
        )
        results.update(batch_results)

    return [results[i] for i in range(len(tool_uses))]


async def run_tool_use(
    tool_use: ToolUse,
    context: ToolUseContext,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
) -> ToolResult:
    """Execute a single tool-use through its 7-step lifecycle:

    1. resolve   — registry lookup
    2. validate  — Pydantic validation
    3. bash_guard — BashTool AST safety check
    4. permission — engine + callback
    5. pre-hooks  — (placeholder — hooks system not yet implemented, Item 9)
    6. execute   — ``tool.call()``
    7. persist   — large results → temp file, truncated preview
    """
    # ---- 1. resolve ---------------------------------------------------------
    try:
        tool = get_tool(tool_use.tool_name)
    except KeyError:
        return ToolResult(
            tool_use_id=tool_use.tool_use_id,
            tool_name=tool_use.tool_name,
            content=f"Unknown tool: {tool_use.tool_name}",
            is_error=True,
        )

    # ---- 2. validate --------------------------------------------------------
    try:
        validated_input = tool.validate_input(tool_use.tool_input)
    except Exception as exc:
        return ToolResult(
            tool_use_id=tool_use.tool_use_id,
            tool_name=tool_use.tool_name,
            content=f"Invalid input: {exc}",
            is_error=True,
        )

    # ---- 3. bash_guard (BashTool only) --------------------------------------
    if tool.name == "Bash":
        command = tool_use.tool_input.get("command", "")
        if command:
            safety = analyze_bash_command(command)
            if not safety.safe:
                return ToolResult(
                    tool_use_id=tool_use.tool_use_id,
                    tool_name=tool_use.tool_name,
                    content=(
                        "Bash command blocked by safety check:\n"
                        + "\n".join(f"  - {w}" for w in safety.warnings)
                    ),
                    is_error=True,
                )

    # ---- 4. permission ------------------------------------------------------
    engine = permission_engine or PermissionEngine()
    decision = await engine.check(
        tool_name=tool_use.tool_name,
        tool_input=tool_use.tool_input,
        callback=permission_callback,
        tool_is_read_only=tool.is_read_only,
        tool_is_destructive=tool.is_destructive,
    )
    if not decision.allowed:
        return ToolResult(
            tool_use_id=tool_use.tool_use_id,
            tool_name=tool_use.tool_name,
            content=f"Permission denied: {decision.reason}",
            is_error=True,
        )

    # ---- 5. pre-hooks (placeholder — Item 9) --------------------------------
    # Hooks system not yet implemented.  When available, iterate registered
    # PreToolUse hooks here; any hook returning "block" terminates early.

    # ---- 6. execute ---------------------------------------------------------
    try:
        result: ToolCallResult = await tool.call(validated_input, context)
    except Exception as exc:
        return ToolResult(
            tool_use_id=tool_use.tool_use_id,
            tool_name=tool_use.tool_name,
            content=f"Tool execution error: {exc}",
            is_error=True,
        )

    # ---- 7. persist — large results → temp file (async I/O) -------------------
    content, is_error = await _persist_large_result(
        result, tool_use.tool_name, context.agent_id,
    )

    return ToolResult(
        tool_use_id=tool_use.tool_use_id,
        tool_name=tool_use.tool_name,
        content=content,
        is_error=is_error,
    )


# ---- internal helpers -------------------------------------------------------


def _tool_is_concurrency_safe(tool_name: str) -> bool:
    """Check ``is_concurrency_safe`` without instantiating the tool."""
    try:
        tool = get_tool(tool_name)
    except KeyError:
        return True  # unknown tools fail fast; safe to treat as concurrent
    return tool.is_concurrency_safe


async def _run_concurrent_batch(
    batch: list[tuple[int, ToolUse]],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None,
    permission_engine: PermissionEngine | None,
    max_concurrency: int,
) -> dict[int, ToolResult]:
    """Execute a batch of concurrency-safe tools in parallel."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(idx: int, tu: ToolUse) -> tuple[int, ToolResult]:
        async with sem:
            result = await run_tool_use(
                tu, context, permission_callback, permission_engine,
            )
        return idx, result

    tasks = [_one(idx, tu) for idx, tu in batch]
    gathered = await asyncio.gather(*tasks)
    return dict(gathered)


async def _persist_large_result(
    result: ToolCallResult,
    tool_name: str,
    agent_id: str,
) -> tuple[object, bool]:
    """If *result.content* is a large string, persist to temp file and truncate.

    Uses ``aiofiles`` for async I/O to avoid blocking the event loop when
    writing multi-MB tool outputs (e.g. Grep on a large codebase).
    """
    content = result.content
    if not isinstance(content, str):
        return content, result.is_error

    byte_len = len(content.encode("utf-8"))
    if byte_len <= _RESULT_TRUNCATION_BYTES:
        return content, result.is_error

    # Build temp path — directory creation via aiofiles.os to stay async
    ts = _time.strftime("%Y%m%d_%H%M%S")
    tmp_dir = tempfile.gettempdir()
    results_dir = f"{tmp_dir}/agent_core/results/{agent_id}"
    await aiofiles.os.makedirs(results_dir, exist_ok=True)

    fname = f"{ts}_{tool_name}_{os.urandom(4).hex()}.txt"
    fpath = f"{results_dir}/{fname}"

    async with aiofiles.open(fpath, "w", encoding="utf-8") as fh:
        await fh.write(content)

    preview = content[:_RESULT_PREVIEW_CHARS]
    truncated = (
        f"{preview}\n\n"
        f"[Content truncated: {_RESULT_PREVIEW_CHARS} chars shown. "
        f"Full result ({byte_len} bytes) saved to: {fpath}]"
    )
    return truncated, False
