"""
AgentTool — LLM-callable tool that spawns sub-agents via ``tool_use``.

When the LLM emits an ``Agent`` tool-use block, this tool resolves the
requested ``AgentDefinition``, sets up the child execution environment,
and routes to either synchronous (inline) or asynchronous (background)
execution.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..tools.base import Tool, ToolCallResult, ToolUseContext
from ..tools.registry import register_tool
from .definition import AgentDefinition
from .registry import get_agent
from .resolve import MAX_AGENT_DEPTH

# ---------------------------------------------------------------------------
# Model name aliases (AgentTool input → full provider/model)
# ---------------------------------------------------------------------------

_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus": "anthropic/claude-opus-4-7",
    "haiku": "anthropic/claude-haiku-4-5",
}

# ---------------------------------------------------------------------------
# AgentInput — validated input schema
# ---------------------------------------------------------------------------


class AgentInput(BaseModel):
    """Validated input for the ``Agent`` tool."""

    description: str = Field(
        description="A short (3-5 word) description of the task."
    )
    prompt: str = Field(
        description="The task for the agent to perform."
    )
    subagent_type: str = Field(
        default="GeneralPurpose",
        description="Agent type, e.g. 'Explore', 'Plan', 'GeneralPurpose'.",
    )
    model: str | None = Field(
        default=None,
        description="Optional model override (alias: sonnet/opus/haiku).",
    )
    run_in_background: bool = Field(
        default=False,
        description="Run in background and notify when done.",
    )


# ---------------------------------------------------------------------------
# BackgroundAgentRegistry — tracks async sub-agent tasks
# ---------------------------------------------------------------------------


class BackgroundAgentRegistry:
    """Per-query registry of in-flight background sub-agent tasks.

    Stored on (or alongside) the parent ``ToolUseContext``.  The parent
    ``query()`` loop calls ``drain()`` at exit to await outstanding tasks
    and collect final notifications.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._notification_queue: list[str] | None = None

    def set_notification_queue(self, queue: list[str]) -> None:
        self._notification_queue = queue

    def launch(self, agent_id: str, coro) -> None:
        """Create and track an ``asyncio.Task`` for *coro*."""
        task = asyncio.ensure_future(coro)  # type: ignore[arg-type]
        self._tasks[agent_id] = task
        task.add_done_callback(lambda t: self._on_done(agent_id, t))

    def _on_done(self, agent_id: str, task: asyncio.Task) -> None:
        """Cleanup callback — log exceptions, remove from registry."""
        self._tasks.pop(agent_id, None)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                import logging
                logging.getLogger("agent_core.agents.tool").warning(
                    "Background agent '%s' failed: %s", agent_id, exc
                )

    async def drain(self) -> None:
        """Wait for all tracked background tasks to finish.

        Called by the parent ``query()`` loop at exit to ensure
        notifications are delivered before the session ends.
        """
        if not self._tasks:
            return
        pending = list(self._tasks.values())
        await asyncio.gather(*pending, return_exceptions=True)

    @property
    def active_count(self) -> int:
        return len(self._tasks)


# ---------------------------------------------------------------------------
# AgentTool
# ---------------------------------------------------------------------------


@register_tool
class AgentTool(Tool):
    """Tool that lets the LLM create sub-agents for complex multi-step tasks.

    The LLM provides ``description``, ``prompt``, and optionally a
    ``subagent_type``.  This tool resolves the agent definition, sets
    up the child execution environment, and routes to either synchronous
    (inline) or asynchronous (background) execution.
    """

    name: ClassVar[str] = "Agent"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    # Destructive: sub-agents may execute Write/Edit/Bash — must NOT be
    # auto-allowed by the engine's "non-destructive fallback" (step 6/7).
    is_destructive: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Tool interface
    # ------------------------------------------------------------------

    def validate_input(self, input: dict[str, Any]) -> AgentInput:
        return AgentInput.model_validate(input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return AgentInput.model_json_schema()

    async def call(
        self, input: AgentInput, context: ToolUseContext
    ) -> ToolCallResult:
        # -- 0. recursion guard (§2.6) ----------------------------------------
        if context.agent_depth >= MAX_AGENT_DEPTH:
            return ToolCallResult(
                content=f"Agent nesting depth limit ({MAX_AGENT_DEPTH}) reached.",
                is_error=True,
            )

        # -- 1. resolve AgentDefinition ---------------------------------------
        try:
            definition = get_agent(input.subagent_type)
        except KeyError:
            return ToolCallResult(
                content=f"Unknown agent type: '{input.subagent_type}'",
                is_error=True,
            )

        # -- 2. model alias translation (§2.5) ---------------------------------
        if input.model is not None:
            full_model = _MODEL_ALIASES.get(
                input.model, input.model
            )
            definition = definition.model_copy(update={"model": full_model})

        # -- 3. validate parent handles (§1.5) ---------------------------------
        if context.parent_config is None:
            return ToolCallResult(
                content="AgentTool requires parent context (parent_config missing).",
                is_error=True,
            )

        agent_id = uuid.uuid4().hex

        # -- 4. SubagentStart hook (§Item 9) -----------------------------------
        hook_registry = getattr(context, "hook_registry", None)
        if hook_registry is not None and not hook_registry.is_empty():
            from ..hooks.pipeline import run_subagent_start_hooks

            blocked, reason = await run_subagent_start_hooks(
                input.subagent_type, input.prompt, definition, context, hook_registry
            )
            if blocked:
                return ToolCallResult(
                    content=f"Sub-agent blocked: {reason or 'denied by hook'}",
                    is_error=True,
                )

        # -- 5. route ---------------------------------------------------------
        if input.run_in_background or definition.background:
            return await self._run_async(agent_id, definition, input.prompt, context)
        return await self._run_sync(agent_id, definition, input.prompt, context)

    # ------------------------------------------------------------------
    # Sync execution path
    # ------------------------------------------------------------------

    async def _run_sync(
        self,
        agent_id: str,
        definition: AgentDefinition,
        prompt: str,
        context: ToolUseContext,
    ) -> ToolCallResult:
        """Inline iteration: drain the child stream, return aggregated text."""
        from .run import run_agent

        parts: list[str] = []
        is_error = False

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
            parent_mcp_servers=context.parent_config.mcp_servers,  # type: ignore[union-attr]
            parent_context=context,
            session_id=context.session_id,
            project_hash=context.project_hash,
        ):
            if msg.type == "assistant_text":
                parts.append(msg.content)
            elif msg.type == "done" and msg.stop_reason == "error":
                is_error = True

        return ToolCallResult(content="\n".join(parts), is_error=is_error)

    # ------------------------------------------------------------------
    # Async execution path
    # ------------------------------------------------------------------

    async def _run_async(
        self,
        agent_id: str,
        definition: AgentDefinition,
        prompt: str,
        context: ToolUseContext,
    ) -> ToolCallResult:
        """Launch tracked background task, return immediately."""
        from .lifecycle import run_async_lifecycle

        registry = _get_or_create_bg_registry(context)
        registry.launch(
            agent_id,
            run_async_lifecycle(
                agent_id=agent_id,
                definition=definition,
                prompt=prompt,
                context=context,
            ),
        )
        return ToolCallResult(
            content=json.dumps(
                {"status": "async_launched", "agent_id": agent_id}
            ),
            is_error=False,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BG_REGISTRY_KEY = "__agent_core_bg_registry__"


_BG_REGISTRY_ATTR = "_bg_registry"


def _get_or_create_bg_registry(
    context: ToolUseContext,
) -> BackgroundAgentRegistry:
    """Get or attach a ``BackgroundAgentRegistry`` to *context*.

    Stored as a plain attribute on the (non-slots) ``ToolUseContext``
    dataclass so that the owning ``query()`` turn loop can retrieve it
    via :func:`get_bg_registry` and ``drain()`` it at exit.
    """
    reg = getattr(context, _BG_REGISTRY_ATTR, None)
    if reg is None:
        reg = BackgroundAgentRegistry()
        if context.notification_queue is not None:
            reg.set_notification_queue(context.notification_queue)
        setattr(context, _BG_REGISTRY_ATTR, reg)
    return reg


def get_bg_registry(context: ToolUseContext) -> BackgroundAgentRegistry | None:
    """Return the ``BackgroundAgentRegistry`` attached to *context*, if any.

    Used by the parent ``query()`` loop to await outstanding background
    sub-agents before terminating.
    """
    return getattr(context, _BG_REGISTRY_ATTR, None)
