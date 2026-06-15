from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from .models import AgentConfig, AgentMessage, AgentResult, StopReason
from .query import query


# Returns True to allow, False to deny. Widen to a PermissionDecision
# structure later if richer outcomes (scoped/remembered approvals) are needed.
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


class Agent:
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._permission_callback: PermissionCallback | None = None
        self._trace_id = uuid.uuid4().hex

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
        async for message in query(
            prompt=prompt,
            config=self._config,
            trace_id=self._trace_id,
            permission_callback=self._permission_callback,
        ):
            yield message
