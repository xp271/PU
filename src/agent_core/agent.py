from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

from .models import AgentConfig, AgentMessage, AgentResult, StopReason
# Single source of truth for the callback type — re-exported here for callers
# that import it from ``agent``.
from .permissions.engine import PermissionCallback
from .query import query
from .session import SessionState

__all__ = ["Agent", "PermissionCallback"]


class Agent:
    def __init__(
        self, config: AgentConfig, session: SessionState | None = None
    ) -> None:
        self._config = config
        self._permission_callback: PermissionCallback | None = None
        self._trace_id = uuid.uuid4().hex
        # When provided, conversation history and session-permission memory
        # persist across prompts; when None, each run is independent.
        self._session = session

    def on_permission_check(self, callback: PermissionCallback) -> None:
        self._permission_callback = callback

    async def run(self, prompt: str) -> AgentResult:
        content_parts: list[str] = []
        total_tokens = 0
        tool_uses = 0
        stop_reason: StopReason = "end_turn"
        error_detail: str | None = None

        async for message in self.stream(prompt):
            if message.type == "assistant_text":
                content_parts.append(message.content)
            elif message.type == "tool_use":
                tool_uses += 1
            elif message.type == "error":
                stop_reason = "error"
                error_detail = message.message
            elif message.type == "done":
                # Terminal message carries authoritative accounting.
                stop_reason = message.stop_reason
                total_tokens = message.total_tokens
                if message.error_detail is not None:
                    error_detail = message.error_detail

        return AgentResult(
            content="".join(content_parts),
            total_tokens=total_tokens,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            error_detail=error_detail,
        )

    async def stream(self, prompt: str) -> AsyncGenerator[AgentMessage, None]:
        # ``aclosing`` guarantees the inner ``query`` generator is closed in
        # *this* task even if the consumer abandons us mid-stream — so MCP
        # teardown (query's finally) never runs at event-loop shutdown in a
        # different task (anyio cancel-scope requirement).
        session = self._session
        async with aclosing(
            query(
                prompt=prompt,
                config=self._config,
                trace_id=self._trace_id,
                permission_callback=self._permission_callback,
                workspace_root=self._config.workspace_root,
                initial_messages=(
                    session.snapshot_for_next_turn() if session is not None else None
                ),
                session_store=session.permission_store if session is not None else None,
            )
        ) as stream:
            async for message in stream:
                # Thread this turn's full history back into the session so the
                # next prompt continues the conversation.
                if session is not None and message.type == "done":
                    session.record_turn_result(message.final_messages)
                yield message
