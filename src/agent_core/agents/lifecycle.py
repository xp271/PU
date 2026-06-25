"""
Asynchronous sub-agent lifecycle management.

Tracks background agent execution, collects progress summaries, and
generates ``<task-notification>`` blocks for delivery to the parent agent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..tools.base import ToolUseContext
from .definition import AgentDefinition
from .run import run_agent

# ---------------------------------------------------------------------------
# AgentLifecycleState
# ---------------------------------------------------------------------------


@dataclass
class AgentLifecycleState:
    """Mutable state shared between the lifecycle loop and the progress
    summarizer task."""

    agent_id: str
    status: Literal["running", "completed", "failed", "killed"] = "running"
    total_tokens: int = 0
    tool_use_count: int = 0
    latest_activity: str = "Initializing..."
    recent_texts: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# run_async_lifecycle
# ---------------------------------------------------------------------------


async def run_async_lifecycle(
    agent_id: str,
    definition: AgentDefinition,
    prompt: str,
    context: ToolUseContext,
) -> None:
    """Run a sub-agent in background and manage its full lifecycle.

    This coroutine is the **sole consumer** of the ``run_agent()``
    message stream — it iterates the generator, updates the shared
    ``AgentLifecycleState``, and the progress summarizer reads from
    that state (never touches the stream directly).

    On completion the final result is extracted, an XML
    ``<task-notification>`` is enqueued, and the state is marked
    ``"completed"`` or ``"failed"``.
    """
    state = AgentLifecycleState(agent_id=agent_id, status="running")

    summarizer = asyncio.ensure_future(_progress_summarizer(state))
    is_error = False

    try:
        transcript: list[dict[str, Any]] = []

        async for msg in run_agent(
            prompt=prompt,
            definition=definition,
            parent_config=context.parent_config,  # type: ignore[arg-type]
            parent_trace_id=context.parent_trace_id,
            parent_permission_callback=context.parent_permission_callback,
            parent_messages=context.messages,
            parent_system=context.parent_system,
            parent_allowed_tools=context.parent_allowed_tools,
            parent_disallowed_tools=context.parent_disallowed_tools,
            parent_mcp_servers=(
                context.parent_config.mcp_servers  # type: ignore[union-attr]
                if context.parent_config
                else {}
            ),
            parent_context=context,
        ):
            _update_state(state, msg)
            if msg.type == "done" and msg.stop_reason == "error":
                is_error = True

        # Set terminal status BEFORE building the notification so the
        # <task-notification> reflects the final state, not "running".
        state.status = "failed" if is_error else "completed"
        finalize_agent_tool(state)

        # Deliver notification
        if context.notification_queue is not None:
            enqueue_agent_notification(state, context.notification_queue)

    except Exception as exc:
        state.error = str(exc)
        state.status = "failed"
        if context.notification_queue is not None:
            enqueue_agent_notification(state, context.notification_queue)

    finally:
        summarizer.cancel()
        try:
            await summarizer
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Progress summarizer
# ---------------------------------------------------------------------------


async def _progress_summarizer(
    state: AgentLifecycleState,
    interval: float = 30.0,
) -> None:
    """Periodically produce a short (3–5 word) progress summary.

    Reads ``state.recent_texts`` — does **not** consume the message
    stream, so there is never contention with ``run_async_lifecycle``.
    """
    while True:
        await asyncio.sleep(interval)
        state.latest_activity = _extract_3_5_words(state.recent_texts)


def _extract_3_5_words(texts: list[str]) -> str:
    """Extract the first 3–5 words from the most recent assistant text."""
    if not texts:
        return "Working..."
    # Take the most recent non-empty text
    for t in reversed(texts):
        words = t.strip().split()
        if words:
            snippet = " ".join(words[:5])
            if len(words) > 5:
                snippet += "..."
            return snippet
    return "Processing..."


# ---------------------------------------------------------------------------
# State update helper
# ---------------------------------------------------------------------------


def _update_state(
    state: AgentLifecycleState, msg: Any
) -> None:
    """Called by ``run_async_lifecycle`` on each message from the stream."""
    msg_type = getattr(msg, "type", "")

    if msg_type == "tool_use":
        state.tool_use_count += 1

    elif msg_type == "assistant_text":
        content = getattr(msg, "content", "")
        if content:
            state.recent_texts.append(str(content)[:200])
            # Keep only the last 10 entries
            if len(state.recent_texts) > 10:
                state.recent_texts = state.recent_texts[-10:]

    elif msg_type == "done":
        state.total_tokens = getattr(msg, "total_tokens", 0)


# ---------------------------------------------------------------------------
# Finalize & notify
# ---------------------------------------------------------------------------


def finalize_agent_tool(
    state: AgentLifecycleState,
) -> AgentLifecycleState:
    """Mark the lifecycle as complete.

    Since result text is collected during the stream iteration,
    this mainly sets the timestamp.  The caller should set
    ``state.result`` before calling if a text summary is needed.
    """
    state.completed_at = time.time()
    if not state.result:
        # Use the latest activity as a minimal result placeholder
        state.result = state.latest_activity
    return state


from xml.sax.saxutils import escape as _xml_escape


def enqueue_agent_notification(
    state: AgentLifecycleState,
    queue: list[str],
) -> str:
    """Build an XML ``<task-notification>`` block and append it to *queue*.

    Dynamic text (result, status) is XML-escaped to prevent
    breaking the notification structure.
    """
    note = (
        "<task-notification>"
        f"<agent_id>{_xml_escape(state.agent_id)}</agent_id>"
        f"<status>{_xml_escape(state.status)}</status>"
        f"<total_tokens>{state.total_tokens}</total_tokens>"
        f"<result>{_xml_escape(state.result)}</result>"
        "</task-notification>"
    )
    queue.append(note)
    return note
