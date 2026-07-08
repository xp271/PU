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
from ..permissions.store import SessionPermissionStore
from ..tools.base import ToolCallResult, ToolUseContext
from ..tools.registry import get_tool, ToolRegistry, _default_registry
from ..mcp.client import McpSessionManager
from ..mcp.registry import register_mcp_tools
from .budget import estimate_tokens_from_messages, get_context_window, TokenBudget
from .context import enrich_context
from .truncate import hard_truncate
from ..compression.trigger import can_compress
from ..hooks.pipeline import run_stop_hooks
from ..skills.loader import discover_skills
from ..skills.registry import SkillRegistry
from ..skills.tool import _set_skill_registry
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
    registry: ToolRegistry | None = None,
    system: str | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    session_store: SessionPermissionStore | None = None,
    hook_registry: Any = None,
    session_id: str | None = None,
    project_hash: str | None = None,
    recorder: Any = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Run the full agent turn loop for a single user prompt.

    Yields every ``AgentMessage`` (text, tool-use, tool-result, errors,
    terminal ``DoneMessage``) so that ``Agent.run()`` and
    ``Agent.stream()`` can both consume the same generator.

    *registry* is an optional ``ToolRegistry`` instance.  When ``None``
    the module-level default is used (backward compat).

    *system* (new, §1.5): External system prompt override.  When not
    ``None`` this string is used as the system prompt instead of
    (in addition to) ``enrich_context()`` output.

    *initial_messages* (new, §1.5): Pre-populated message history for
    fork / resume paths.  When not ``None``, the turn loop starts from
    this message list instead of a fresh ``[_build_initial_user_message(prompt)]``.
    """

    # ---- 0. Tool registry (query-scoped) ------------------------------------
    if registry is None:
        registry = _default_registry

    # ---- 0b. MCP lifecycle -------------------------------------------------
    # The session manager owns anyio cancel scopes that MUST be torn down in
    # the same task that opened them.  Wrapping the whole turn loop in
    # try/finally guarantees aclose() runs here on EVERY exit path (normal,
    # early return, error, GeneratorExit) — instead of leaking sessions to
    # asyncio.run() shutdown, where finalization runs in a different task and
    # raises "Attempted to exit cancel scope in a different task".
    mcp = McpSessionManager()
    try:
        await _bootstrap_mcp(mcp, config, registry)

        async for message in _run_turns(
            prompt=prompt,
            config=config,
            trace_id=trace_id,
            permission_callback=permission_callback,
            workspace_root=workspace_root,
            registry=registry,
            system=system,
            initial_messages=initial_messages,
            session_store=session_store,
            hook_registry=hook_registry,
            session_id=session_id,
            project_hash=project_hash,
            recorder=recorder,
        ):
            yield message
    finally:
        await mcp.aclose()


async def _bootstrap_mcp(
    mcp: McpSessionManager,
    config: AgentConfig,
    registry: ToolRegistry,
) -> None:
    """Connect every configured MCP server and register its tools.

    Failures are logged and skipped so one bad server never blocks the query.
    """
    if not config.mcp_servers:
        return
    for server_name, server_config in config.mcp_servers.items():
        try:
            state = await mcp.connect(server_name, server_config)
            if state.connection_error is None:
                register_mcp_tools(registry, server_name, state)
            else:
                logger.warning(
                    "MCP server '%s' unavailable (%s) — continuing without it",
                    server_name,
                    state.connection_error,
                )
        except Exception as exc:
            logger.warning(
                "MCP bootstrap error for '%s' (skipping): %s",
                server_name,
                exc,
            )


async def _run_turns(
    prompt: str,
    config: AgentConfig,
    trace_id: str,
    permission_callback: PermissionCallback | None,
    workspace_root: str | None,
    registry: ToolRegistry,
    system: str | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    session_store: SessionPermissionStore | None = None,
    hook_registry: Any = None,
    session_id: str | None = None,
    project_hash: str | None = None,
    recorder: Any = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Run the agent turn loop.

    MCP ownership lives in :func:`query`; this generator never touches the
    session manager, so its many early-return paths are safe.
    """
    # ---- 1. Initialisation (runs once) -------------------------------------
    # §1.5: external system param takes precedence; augment with env context
    env_ctx = await enrich_context(workspace_root)
    if system is not None:
        system = (system + "\n\n" + env_ctx) if env_ctx else system
    else:
        system = env_ctx

    # §1.5: fork / resume paths supply message history
    if initial_messages is not None:
        messages: list[dict[str, Any]] = list(initial_messages)
        # Append the current prompt as final user message
        if prompt:
            messages.append(_build_initial_user_message(prompt))
    else:
        messages = [_build_initial_user_message(prompt)]

    # §1.5: compute parent-allowed tools for AgentTool inheritance
    if config.tools == ["*"]:
        parent_allowed = frozenset(registry.names())
    else:
        parent_allowed = frozenset(config.tools)

    notification_queue: list[str] = []

    # §Item 9: ensure a per-query HookRegistry exists even when query() is
    # invoked directly (CLI / tests / fork).  When the caller (Agent.stream)
    # already supplied one it is used as-is; otherwise we derive a fresh
    # instance from the read-only template plus this config's hooks.  This
    # also gives plugin/config hooks a registry to inject into.
    if hook_registry is None:
        from ..hooks.registry import _default_hook_registry

        hook_registry = _default_hook_registry.copy_with_overrides(config.hooks)

    tool_context = ToolUseContext(
        abort_signal=asyncio.Event(),
        read_file_state={},
        messages=messages,
        agent_id=trace_id,
        workspace_root=workspace_root,
        # -- §1.5: parent-agent handles for AgentTool --
        parent_config=config,
        parent_trace_id=trace_id,
        parent_permission_callback=permission_callback,
        parent_system=system,
        parent_allowed_tools=parent_allowed,
        parent_disallowed_tools=frozenset(),
        agent_depth=0,
        notification_queue=notification_queue,
        hook_registry=hook_registry,
        session_id=session_id,
        project_hash=project_hash,
    )

    # §Item 9: bootstrap skill registry (per-query instance).
    # Discovery touches the filesystem; offload to a thread so the event
    # loop is never blocked during startup.  Precedence is first-wins:
    # config.skills_dir > project (.agent/skills) > user (~/.agent/skills).
    skill_registry = SkillRegistry()
    search_dirs: list[tuple[str, str]] = []
    if config.skills_dir:
        search_dirs.append((config.skills_dir, "config"))
    if workspace_root:
        search_dirs.append(
            (os.path.join(workspace_root, ".agent", "skills"), "project")
        )
    search_dirs.append(
        (os.path.join(os.path.expanduser("~"), ".agent", "skills"), "user")
    )
    for directory, source in search_dirs:
        discovered = await asyncio.to_thread(discover_skills, directory)
        for name, sd in discovered.items():
            if skill_registry.contains(name):
                continue  # first-wins: higher-precedence dir already supplied it
            skill_registry.register(sd, source=source)
    _set_skill_registry(tool_context, skill_registry)

    # §Item 9: bootstrap plugins — contributions flow into the per-query
    # hook/skill registries (and the query tool registry / global agent
    # registry).  Failures in any one plugin are logged and skipped.
    if config.auto_enable_plugins:
        from ..agents.registry import _default_registry as _agent_registry
        from ..plugins.lifecycle import safe_enable_plugins
        from ..plugins.registry import PluginRegistry

        plugin_registry = PluginRegistry()
        plugin_registry.discover()
        await safe_enable_plugins(
            plugin_registry,
            config.auto_enable_plugins,
            tool_registry=registry,
            hook_registry=hook_registry,
            skill_registry=skill_registry,
            agent_registry=_agent_registry,
        )

    model_id = config.model_id

    # -- llamacpp: ensure local server is running before first request -------
    logger.debug("query: model=%s provider=%s — ensuring llamacpp server",
                 model_id, model_id.provider)
    from ..llm.llamacpp import ensure_server  # noqa: PLC0415
    await ensure_server(config)

    client = get_client(model_id, config)
    tools_schema = client.__class__.convert_tools(
        config.tools, registry=registry
    )

    permission_engine = PermissionEngine.from_config(
        permission_mode=config.permission_mode,
        session_store=session_store,
    )

    budget = TokenBudget(
        max_turns=config.max_turns,
        context_window=get_context_window(model_id, config_override=config.context_size),
    )

    total_tokens = 0
    error_detail: str | None = None
    _output_truncated_retries = 0
    _OUTPUT_TRUNCATED_MAX_RETRIES = 3

    # ---- 2. Turn loop ------------------------------------------------------
    for turn_index in range(1, config.max_turns + 1):

        # -- 2a0. Notification queue drain (§1.5 / §5.5) -------------------
        if notification_queue:
            pending = "\n".join(notification_queue)
            notification_queue.clear()
            messages.append({
                "role": "user",
                "content": [{"type": "text", "text": pending}],
            })

        # -- 2a0.5. Stop hook (§Item 9) ----------------------------------------
        if hook_registry is not None and hook_registry.stop_hooks():
            stop_result = await run_stop_hooks(messages, tool_context, hook_registry)
            if stop_result.should_stop:
                if stop_result.message:
                    yield AssistantText(content=stop_result.message)
                await _drain_background_agents(tool_context)
                yield DoneMessage(
                    stop_reason="end_turn",
                    total_tokens=total_tokens,
                    final_messages=messages,
                )
                return

        # -- 2a. Budget / truncation check before the LLM call --------------
        if budget.is_exhausted():
            await _drain_background_agents(tool_context)
            yield DoneMessage(
                stop_reason="max_turns",
                total_tokens=total_tokens,
                final_messages=messages,
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
                    budget.reset_last_actual()

                    # Compression succeeded but might still be over threshold
                    if budget.needs_truncation(estimated):
                        messages = hard_truncate(
                            messages, max_tokens=budget.truncation_threshold
                        )
                        tool_context.messages = messages
                        budget.reset_last_actual()

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
                    budget.reset_last_actual()

            else:
                # Compression unavailable — direct truncation
                messages = hard_truncate(
                    messages, max_tokens=budget.truncation_threshold
                )
                tool_context.messages = messages
                budget.reset_last_actual()

        # -- 2b. LLM call ---------------------------------------------------
        try:
            tracer = TurnTracer(trace_id=trace_id, recorder=recorder)
            raw_stream = client.stream_messages(
                messages=messages,
                tools=tools_schema,
                system=system if system else None,
                max_tokens=10000,
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
                        await _drain_background_agents(tool_context)
                        yield DoneMessage(
                            stop_reason="error",
                            total_tokens=total_tokens,
                            error_detail=event.message,  # type: ignore[attr-defined]
                            final_messages=messages,
                        )
                        return

        except Exception as exc:
            yield ErrorMessage(message=str(exc), error_type=type(exc).__name__)
            await _drain_background_agents(tool_context)
            yield DoneMessage(
                stop_reason="error",
                total_tokens=total_tokens,
                error_detail=str(exc),
                final_messages=messages,
            )
            return

        # -- 2c. Build & append assistant message ---------------------------
        messages.append(
            _build_assistant_message(assistant_texts, tool_uses_in_turn)
        )

        # -- 2c5. Output truncated → roll back & retry with limit prompt -----
        if turn_stop_reason == "output_truncated":
            _output_truncated_retries += 1
            if _output_truncated_retries > _OUTPUT_TRUNCATED_MAX_RETRIES:
                yield DoneMessage(
                    stop_reason="error",
                    total_tokens=total_tokens,
                    error_detail=(
                        f"Output truncated {_output_truncated_retries} times "
                        f"(max {_OUTPUT_TRUNCATED_MAX_RETRIES}); giving up."
                    ),
                    final_messages=messages,
                )
                return
            # Remove the truncated incomplete message
            messages.pop()
            # Instruct the model to continue concisely
            messages.append({
                "role": "user",
                "content": [{"type": "text", "text": (
                    "Your previous response was truncated because it exceeded "
                    "the output length limit. Please continue from where you "
                    "left off, keeping your output within the limit."
                )}],
            })
            budget.reset_diminishing_returns()
            continue

        # -- 2d. No tool uses → terminal turn -------------------------------
        if not tool_uses_in_turn:
            # Before ending, await any pending background sub-agents so their
            # <task-notification> results can be delivered on a further turn
            # (and so the tasks are never orphaned at event-loop shutdown).
            reg = _bg_registry(tool_context)
            if reg is not None and reg.active_count > 0:
                await reg.drain()
                if notification_queue:
                    # The 2a0 checkpoint will inject the notifications next turn.
                    continue
            stop_reason = turn_stop_reason
            if stop_reason == "permission_denied":
                pass  # preserve
            yield DoneMessage(
                stop_reason=stop_reason,  # type: ignore[arg-type]
                total_tokens=total_tokens,
                error_detail=error_detail,
                final_messages=messages,
            )
            return

        # -- 2e. Execute tools & yield results ------------------------------
        tool_results = await run_tools(
            tool_uses_in_turn,
            tool_context,
            permission_callback=permission_callback,
            permission_engine=permission_engine,
            max_concurrency=config.max_concurrency,
            registry=registry,
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
    await _drain_background_agents(tool_context)
    yield DoneMessage(
        stop_reason="max_turns",
        total_tokens=total_tokens,
        final_messages=messages,
    )


# ---------------------------------------------------------------------------
# Message builders (Anthropic content-block format)
# ---------------------------------------------------------------------------


def _bg_registry(context: ToolUseContext):
    """Return the background sub-agent registry attached to *context* (or None).

    Lazy import avoids any import cycle with the agents package.
    """
    from ..agents.tool import get_bg_registry

    return get_bg_registry(context)


async def _drain_background_agents(context: ToolUseContext) -> None:
    """Await any in-flight background sub-agents so they are never orphaned."""
    reg = _bg_registry(context)
    if reg is not None:
        await reg.drain()


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
    registry: ToolRegistry | None = None,
) -> list[ToolResult]:
    """Execute a batch of tool-use requests with concurrency orchestration.

    Non-concurrency-safe tools run serially; concurrency-safe tools run
    in parallel with an ``asyncio.Semaphore`` cap.  Results are returned
    in the same order as *tool_uses*.

    **Signature** — compatible replacement for the old
    ``_execute_tools_sequential``.
    """
    if registry is None:
        registry = _default_registry

    # Fast path: single tool — no concurrency overhead
    if len(tool_uses) == 1:
        tu = tool_uses[0]
        return [await run_tool_use(tu, context, permission_callback, permission_engine, registry)]

    results: dict[int, ToolResult] = {}
    pending_batch: list[tuple[int, ToolUse]] = []

    for idx, tu in enumerate(tool_uses):
        is_concurrent = _tool_is_concurrency_safe(tu.tool_name, registry)

        if is_concurrent:
            pending_batch.append((idx, tu))
        else:
            # Flush pending concurrent batch first
            if pending_batch:
                batch_results = await _run_concurrent_batch(
                    pending_batch, context, permission_callback,
                    permission_engine, max_concurrency, registry,
                )
                results.update(batch_results)
                pending_batch.clear()
            # Execute serial tool
            results[idx] = await run_tool_use(
                tu, context, permission_callback, permission_engine, registry,
            )

    # Flush remaining concurrent batch
    if pending_batch:
        batch_results = await _run_concurrent_batch(
            pending_batch, context, permission_callback,
            permission_engine, max_concurrency, registry,
        )
        results.update(batch_results)

    return [results[i] for i in range(len(tool_uses))]


async def run_tool_use(
    tool_use: ToolUse,
    context: ToolUseContext,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
    registry: ToolRegistry | None = None,
) -> ToolResult:
    """Execute a single tool-use through its lifecycle:

    1. resolve   — registry lookup
    2. validate  — Pydantic validation
    3. bash_guard — BashTool AST safety check
    4. permission — engine + callback
    5. pre-hooks  — PreToolUseHook chain (block/modify/continue)
    6. execute   — ``tool.call()``
    7. post-hooks — PostToolUseHook chain (before persist)
    8. persist   — large results → temp file, truncated preview
    """
    if registry is None:
        registry = _default_registry

    # ---- 1. resolve ---------------------------------------------------------
    try:
        tool = registry.get(tool_use.tool_name)
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

    # ---- 3~5. guard + permission + pre-hooks (converge on effective_input) --
    #
    # Helper: run bash_guard + permission on *inp*.  Returns an error
    # ToolResult when the checks fail, or None when they pass.
    async def _guard_and_permit(inp: dict[str, Any]) -> ToolResult | None:
        # 3. bash_guard (BashTool only)
        if tool.name == "Bash":
            command = inp.get("command", "")
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
        # 4. permission
        decision = await engine.check(
            tool_name=tool_use.tool_name,
            tool_input=inp,
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
        return None

    engine = permission_engine or PermissionEngine()
    effective_input: dict[str, Any] = tool_use.tool_input

    # First guard + permit (on original input)
    err = await _guard_and_permit(effective_input)
    if err is not None:
        return err

    # 5. pre-hooks (§Item 9)
    hook_registry = getattr(context, "hook_registry", None)
    if hook_registry is not None and not hook_registry.is_empty():
        from ..hooks.pipeline import run_pre_tool_use_hooks

        modified_input, block_reason = await run_pre_tool_use_hooks(
            tool_use.tool_name, effective_input, context, hook_registry
        )
        if block_reason is not None:
            return ToolResult(
                tool_use_id=tool_use.tool_use_id,
                tool_name=tool_use.tool_name,
                content=f"Tool blocked by pre-use hook: {block_reason}",
                is_error=True,
            )
        if modified_input is not None:
            effective_input = modified_input
            # Re-validate + re-run guard/permission on the hook-modified input
            try:
                validated_input = tool.validate_input(effective_input)
            except Exception as exc:
                return ToolResult(
                    tool_use_id=tool_use.tool_use_id,
                    tool_name=tool_use.tool_name,
                    content=f"Invalid input (after pre-hook modify): {exc}",
                    is_error=True,
                )
            err = await _guard_and_permit(effective_input)
            if err is not None:
                return err

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

    # ---- 7. post-hooks (§Item 9 — before persist, on full ToolCallResult) ---
    if hook_registry is not None and not hook_registry.is_empty():
        from ..hooks.pipeline import run_post_tool_use_hooks

        result = await run_post_tool_use_hooks(
            tool_use.tool_name, effective_input, result, context, hook_registry
        )

    # ---- 8. persist — large results → temp file (async I/O) -------------------
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


def _tool_is_concurrency_safe(tool_name: str, registry: ToolRegistry | None = None) -> bool:
    """Check ``is_concurrency_safe`` without instantiating the tool."""
    if registry is None:
        registry = _default_registry
    try:
        tool = registry.get(tool_name)
    except KeyError:
        return True  # unknown tools fail fast; safe to treat as concurrent
    return tool.is_concurrency_safe


async def _run_concurrent_batch(
    batch: list[tuple[int, ToolUse]],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None,
    permission_engine: PermissionEngine | None,
    max_concurrency: int,
    registry: ToolRegistry | None = None,
) -> dict[int, ToolResult]:
    """Execute a batch of concurrency-safe tools in parallel."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(idx: int, tu: ToolUse) -> tuple[int, ToolResult]:
        async with sem:
            result = await run_tool_use(
                tu, context, permission_callback, permission_engine, registry,
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
