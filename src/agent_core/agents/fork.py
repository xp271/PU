"""
Fork & resume support for sub-agents.

- ``filter_incomplete_tool_calls()`` removes dangling tool_use blocks.
- ``fork_messages()`` prepares parent messages for forking a sub-agent.
- ``resume_agent()`` reconstructs state from a sidechain JSONL transcript.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import aiofiles

from ..models import (
    AgentConfig,
    AgentMessage,
    McpServerConfig,
)
from ..tools.base import ToolUseContext
from .definition import AgentDefinition
from .run import run_agent

logger = logging.getLogger("agent_core.agents.fork")


# ---------------------------------------------------------------------------
# filter_incomplete_tool_calls
# ---------------------------------------------------------------------------


def filter_incomplete_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove ``tool_use`` content blocks whose ``tool_result`` is missing.

    Algorithm:
        1. Scan all messages for ``tool_use`` IDs and ``tool_result``
           ``tool_use_id`` values.
        2. Build a set of "completed" tool-use IDs.
        3. For each assistant message, keep only ``tool_use`` blocks
           whose ID appears in the completed set.
        4. Remove assistant messages that become empty after filtering.

    Returns a new list; does not mutate the input.
    """
    # Collect all completed tool_use IDs
    completed_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        for block in _safe_content(msg):
            if block.get("type") == "tool_result":
                tu_id = block.get("tool_use_id", "")
                if tu_id:
                    completed_ids.add(tu_id)

    # Filter
    result: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            raw_blocks = _safe_content(msg)
            filtered_blocks: list[dict[str, Any]] = []
            for block in raw_blocks:
                if block.get("type") == "tool_use":
                    if block.get("id", "") in completed_ids:
                        filtered_blocks.append(block)
                else:
                    filtered_blocks.append(block)
            # Drop empty assistant messages
            if not filtered_blocks:
                continue
            result.append({**msg, "content": filtered_blocks})
        else:
            result.append(msg)

    return result


# ---------------------------------------------------------------------------
# fork_messages
# ---------------------------------------------------------------------------


def fork_messages(
    parent_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prepare parent message history for a forked sub-agent.

    Equivalent to ``filter_incomplete_tool_calls(parent_messages)``
    applied to a shallow copy.  The fork inherits the parent's full
    conversation context, minus any in-progress tool invocations.
    """
    return filter_incomplete_tool_calls(list(parent_messages))


# ---------------------------------------------------------------------------
# resume_agent
# ---------------------------------------------------------------------------


async def resume_agent(
    agent_id: str,
    sidechain_path: str,
    definition: AgentDefinition,
    *,
    parent_config: AgentConfig,
    parent_trace_id: str,
    parent_permission_callback: Any = None,
    parent_system: str = "",
    parent_allowed_tools: frozenset[str] | None = None,
    parent_disallowed_tools: frozenset[str] | None = None,
    parent_mcp_servers: dict[str, McpServerConfig] | None = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Resume a previously started sub-agent from its sidechain JSONL.

    1. Load transcript lines from *sidechain_path*.
    2. Reconstruct the messages list.
    3. Feed into ``run_agent()`` via ``parent_messages`` (fork mode),
       with an empty prompt (the task is already in the messages).

    *read_file_state* is reset to ``{}`` to force re-reading of any
    files that the resumed agent intends to edit (see ``FileEditTool``).
    """
    messages = await _load_sidechain_messages(sidechain_path)
    if not messages:
        logger.warning(
            "resume_agent(%s): empty or missing sidechain at '%s'",
            agent_id, sidechain_path,
        )

    async for msg in run_agent(
        prompt="",   # task is already in the reconstructed messages
        definition=definition,
        parent_config=parent_config,
        parent_trace_id=parent_trace_id,
        parent_permission_callback=parent_permission_callback,
        parent_messages=messages,
        parent_system=parent_system,
        parent_allowed_tools=parent_allowed_tools,
        parent_disallowed_tools=parent_disallowed_tools,
        parent_mcp_servers=parent_mcp_servers,
        parent_context=_stub_resume_context(),
    ):
        yield msg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_content(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the content list from a message, handling edge cases."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return []
    return content  # type: ignore[return-value]


async def _load_sidechain_messages(
    sidechain_path: str,
) -> list[dict[str, Any]]:
    """Load and reconstruct a messages list from a sidechain JSONL file."""
    messages: list[dict[str, Any]] = []
    try:
        async with aiofiles.open(sidechain_path, "r", encoding="utf-8") as fh:
            async for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                    messages.append(record)
                except json.JSONDecodeError:
                    logger.debug("Skipping unparseable sidechain line")
                    continue
    except FileNotFoundError:
        logger.warning("Sidechain file not found: %s", sidechain_path)
    return messages


def _stub_resume_context() -> ToolUseContext:
    """Create a minimal stub context for ``resume_agent()``."""
    import asyncio
    return ToolUseContext(
        abort_signal=asyncio.Event(),
        read_file_state={},   # stale — force re-reads
        agent_depth=1,
    )
