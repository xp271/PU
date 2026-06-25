"""
Child-agent execution engine — ``run_agent()`` async generator.

This is the runtime entry point for all sub-agent execution paths
(sync, async background, fork).  It wraps ``query()`` with:

- Isolated ``ToolUseContext``
- Tool-pool filtering & permission-engine construction
- MCP server merging & dedicated connections
- System prompt assembly
- Sidechain JSONL transcription
- ``finally`` cleanup (MCP, hooks, temp files)
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time as _time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import aiofiles
import aiofiles.os

from ..hooks.registry import HookRegistry
from ..models import (
    AgentConfig,
    AgentMessage,
    AssistantText,
    DoneMessage,
    ErrorMessage,
    McpServerConfig,
    ModelId,
    ToolResult,
    ToolUse,
)
from ..permissions.engine import PermissionCallback
from ..permissions.inheritance import compute_child_permission_mode
from ..query import query as run_query
from ..tools.base import Tool, ToolUseContext
from ..tools.registry import ToolRegistry
from .definition import AgentDefinition
from .resolve import (
    build_child_engine,
    build_child_system_prompt,
    disallow_agent_at_depth_limit,
    merge_mcp_servers,
    resolve_agent_tools,
    resolve_model,
)

logger = logging.getLogger("agent_core.agents.run")

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(
    prompt: str,
    definition: AgentDefinition,
    *,
    parent_config: AgentConfig,
    parent_trace_id: str,
    parent_permission_callback: PermissionCallback | None = None,
    parent_messages: list[dict[str, Any]] | None = None,
    parent_system: str = "",
    parent_allowed_tools: frozenset[str] | None = None,
    parent_disallowed_tools: frozenset[str] | None = None,
    parent_mcp_servers: dict[str, McpServerConfig] | None = None,
    parent_context: ToolUseContext | None = None,
    session_id: str | None = None,
    project_hash: str | None = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Execute a child agent in an isolated environment.

    Yields ``AgentMessage`` items from the child's turn loop.  Callers
    (``AgentTool._run_sync``, ``run_async_lifecycle``) can consume or
    aggregate the stream.

    Parameters:
        prompt: Task prompt for the sub-agent.
        definition: Merged ``AgentDefinition`` from the registry.
        parent_config: Parent agent's ``AgentConfig`` (API keys, model, ...).
        parent_trace_id: Parent's trace ID for linking.
        parent_permission_callback: Parent's ask callback (propagated).
        parent_messages: Parent message history (for fork mode).
        parent_system: Parent's effective system prompt.
        parent_allowed_tools: Parent's effective allowed tool set.
        parent_disallowed_tools: Parent's effective disallowed tool set.
        parent_mcp_servers: Parent's active MCP server configs.
        parent_context: Parent's ``ToolUseContext`` for constructing
            the child context (isolation + depth tracking).
    """
    # ---- defaults ---------------------------------------------------------
    if parent_allowed_tools is None:
        parent_allowed_tools = frozenset()
    if parent_disallowed_tools is None:
        parent_disallowed_tools = frozenset()
    if parent_mcp_servers is None:
        parent_mcp_servers = {}

    agent_id = uuid.uuid4().hex

    # ---- 1. Resolve configuration ------------------------------------------
    model_id: ModelId = resolve_model(definition, parent_config.model_id)

    child_allowed, child_disallowed = resolve_agent_tools(
        definition, parent_allowed_tools, parent_disallowed_tools
    )

    # §2.6: depth-limit hard-gate
    depth = (parent_context.agent_depth + 1) if parent_context else 1
    child_allowed = disallow_agent_at_depth_limit(child_allowed, depth)

    # §2.2: clamp permission mode (child can never be more permissive than
    # parent).  This is the SAME mode used to build the child engine; it must
    # also drive the child query()'s internal engine — otherwise a bypass
    # parent would short-circuit step 5 and the clamped mode / auto-approve
    # rules would never be consulted.
    child_mode = compute_child_permission_mode(
        parent_config.permission_mode, definition.permission_mode
    )

    child_engine = build_child_engine(
        definition, parent_config.permission_mode, parent_disallowed_tools
    )

    child_mcp = merge_mcp_servers(definition, parent_mcp_servers)
    child_system = build_child_system_prompt(
        definition, parent_system, child_allowed
    )

    # ---- 2. Isolated ToolUseContext ----------------------------------------
    # §Item 9: child inherits parent hooks; extra hooks from the definition
    # are appended.
    parent_hr = (
        getattr(parent_context, "hook_registry", None)
        if parent_context
        else None
    )
    if parent_hr is not None:
        child_hook_registry: HookRegistry = parent_hr.copy_with_overrides(
            definition.hooks
        )
    else:
        child_hook_registry = HookRegistry()
        child_hook_registry.register_all(definition.hooks)

    notification_queue: list[str] = []

    child_context = ToolUseContext(
        abort_signal=asyncio.Event(),
        read_file_state=(
            copy.deepcopy(parent_context.read_file_state)
            if parent_context
            else {}
        ),
        messages=[],  # populated by query()
        agent_id=agent_id,
        workspace_root=parent_context.workspace_root if parent_context else None,
        # -- parent handles for nested AgentTool calls --
        parent_config=parent_config,
        parent_trace_id=f"{parent_trace_id}/{agent_id}",
        parent_permission_callback=parent_permission_callback,
        parent_system=child_system,
        parent_allowed_tools=child_allowed,
        parent_disallowed_tools=child_disallowed,
        agent_depth=depth,
        notification_queue=notification_queue,
        hook_registry=child_hook_registry,
    )

    # ---- 3. Child AgentConfig ----------------------------------------------
    child_config = parent_config.model_copy(update={
        "model": str(model_id),
        "tools": list(child_allowed),
        "permission_mode": child_mode,
        "mcp_servers": child_mcp,
    })

    # ---- 4. Child ToolRegistry ---------------------------------------------
    child_registry = ToolRegistry()
    _populate_child_registry(child_registry, child_allowed, parent_context)

    # ---- 5. Child permission callback wrapper ------------------------------
    async def _child_perm_cb(tool_name: str, tool_input: dict) -> bool:
        tool = _safe_get_tool(child_registry, tool_name)
        decision = await child_engine.check(
            tool_name=tool_name,
            tool_input=tool_input,
            callback=parent_permission_callback,
            tool_is_read_only=tool.is_read_only if tool else False,
            tool_is_destructive=tool.is_destructive if tool else False,
        )
        return decision.allowed

    # ---- 6. Sidechain file ------------------------------------------------
    sidechain_dir = _sidechain_dir(
        parent_trace_id, session_id=session_id, project_hash=project_hash
    )
    await aiofiles.os.makedirs(sidechain_dir, exist_ok=True)
    sidechain_path = os.path.join(sidechain_dir, f"{agent_id}.jsonl")
    sidechain_fh = await aiofiles.open(sidechain_path, "a", encoding="utf-8")

    # ---- 7. Run query() → aggregate → sidechain → yield -------------------
    try:
        turn_counter = 0
        turn_texts: list[str] = []
        turn_tool_uses: list[ToolUse] = []
        last_su: str | None = None  # sidechain parentUuid chain

        async for msg in run_query(
            prompt=prompt,
            config=child_config,
            trace_id=f"{parent_trace_id}/{agent_id}",
            permission_callback=_child_perm_cb,
            workspace_root=child_context.workspace_root,
            registry=child_registry,
            system=child_system,
            initial_messages=parent_messages,
            hook_registry=child_hook_registry,
        ):
            match msg.type:
                case "assistant_text":
                    turn_texts.append(msg.content)
                    yield msg

                case "tool_use":
                    turn_tool_uses.append(msg)  # type: ignore[arg-type]
                    yield msg

                case "tool_result":
                    # Flush accumulated turn: write assistant + tool results
                    if turn_texts or turn_tool_uses:
                        new_uuid = await _write_turn_to_sidechain(
                            sidechain_fh, turn_counter, turn_texts,
                            turn_tool_uses, session_id, last_su,
                        )
                        if new_uuid:
                            last_su = new_uuid
                    # Write tool_result as user message
                    new_uuid = await _write_tool_result_to_sidechain(
                        sidechain_fh, turn_counter, msg,  # type: ignore[arg-type]
                        session_id, last_su,
                    )
                    if new_uuid:
                        last_su = new_uuid
                    turn_counter += 1
                    turn_texts = []
                    turn_tool_uses = []
                    yield msg

                case "done":
                    # Final flush if turn not yet written
                    if turn_texts or turn_tool_uses:
                        new_uuid = await _write_turn_to_sidechain(
                            sidechain_fh, turn_counter, turn_texts,
                            turn_tool_uses, session_id, last_su,
                        )
                        if new_uuid:
                            last_su = new_uuid
                    yield msg

                case _:
                    yield msg

    finally:
        # ---- 8. Cleanup ----------------------------------------------------
        try:
            await sidechain_fh.flush()
        except Exception:
            pass
        try:
            await sidechain_fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sidechain_dir(
    parent_trace_id: str,
    session_id: str | None = None,
    project_hash: str | None = None,
) -> str:
    """Compute the sidechain directory for a parent session.

    When *session_id* and *project_hash* are both set, uses the new
    Phase B path scheme: ``~/.agent/projects/{hash}/{session_id}/``.
    Otherwise falls back to the legacy path:
    ``~/.agent/sessions/{parent_trace_id}/``.
    """
    home = os.path.expanduser("~")
    if session_id is not None and project_hash is not None:
        return os.path.join(
            home, ".agent", "projects", project_hash, session_id
        )
    return os.path.join(home, ".agent", "sessions", parent_trace_id)


def _populate_child_registry(
    child_registry: ToolRegistry,
    child_allowed: frozenset[str],
    parent_context: ToolUseContext | None,
) -> None:
    """Copy allowed tool classes from the parent context into the child registry.

    For a fresh ``ToolRegistry()`` we re-register tools from the default
    registry.  Tool classes carry no per-instance mutable state, so this
    is safe.
    """
    from ..tools.registry import _default_registry

    for tool_name in child_allowed:
        try:
            tool_cls = type(_default_registry.get(tool_name))
            child_registry.register(tool_cls)
        except KeyError:
            logger.debug("Tool '%s' not found in default registry — skipping", tool_name)


def _safe_get_tool(
    registry: ToolRegistry, tool_name: str
) -> Tool | None:
    """Look up a tool by name, returning ``None`` on failure."""
    try:
        return registry.get(tool_name)
    except KeyError:
        return None


async def _write_turn_to_sidechain(
    fh,
    turn: int,
    texts: list[str],
    tool_uses: list[ToolUse],
    session_id: str | None = None,
    last_uuid: str | None = None,
) -> str | None:
    """Write an assistant message to the sidechain JSONL.

    Uses the ``TranscriptEntry`` envelope (Phase B) when *session_id*
    is provided; otherwise writes the legacy bare format for backward
    compatibility.

    Returns the new entry's UUID (or ``None`` in legacy mode).
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
    if not content:
        return None

    if session_id is not None:
        from ..persistence.storage import TranscriptEntry

        entry = TranscriptEntry.from_message(
            message={"role": "assistant", "content": content},
            session_id=session_id,
            parent_uuid=last_uuid,
            is_sidechain=True,
        )
        await fh.write(entry.model_dump_json() + "\n")
        await fh.flush()
        return entry.uuid

    # Legacy format
    record = {
        "role": "assistant",
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn": turn,
    }
    await fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    await fh.flush()
    return None


async def _write_tool_result_to_sidechain(
    fh,
    turn: int,
    tr: ToolResult,
    session_id: str | None = None,
    last_uuid: str | None = None,
) -> str | None:
    """Write a tool_result user message to the sidechain JSONL.

    Uses the ``TranscriptEntry`` envelope (Phase B) when *session_id*
    is provided.
    """
    if session_id is not None:
        from ..persistence.storage import TranscriptEntry

        entry = TranscriptEntry.from_message(
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_use_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                ],
            },
            session_id=session_id,
            parent_uuid=last_uuid,
            is_sidechain=True,
        )
        await fh.write(entry.model_dump_json() + "\n")
        await fh.flush()
        return entry.uuid

    # Legacy format
    record = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tr.tool_use_id,
                "content": tr.content,
                "is_error": tr.is_error,
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn": turn,
    }
    await fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    await fh.flush()
    return None
