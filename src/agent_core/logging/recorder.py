"""
ConversationRecorder — translates the ``AgentMessage`` stream (and the
per-turn metric payload from :class:`TurnTracer`) into :class:`LogRecord`
objects and forwards them to a :class:`LogSink`.

This is the bypass object mounted in ``Agent.stream`` (see ``agent.py``).  It
is strictly read-only with respect to the conversation: it observes messages,
it never mutates them or influences control flow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import AgentMessage
from .config import LoggingConfig
from .events import (
    AssistantTextRecord,
    ErrorRecord,
    SessionEndRecord,
    SessionStartRecord,
    ToolResultRecord,
    ToolUseRecord,
    TurnMetricRecord,
    UserPromptRecord,
)
from .redact import Redactor
from .sink import LogSink

if TYPE_CHECKING:
    from ..models import AgentConfig

__all__ = ["ConversationRecorder"]


class ConversationRecorder:
    """Observes a conversation and records it via a :class:`LogSink`."""

    def __init__(
        self,
        sink: LogSink,
        *,
        trace_id: str,
        session_id: str | None,
        project_hash: str,
        config: LoggingConfig,
        redactor: Redactor | None = None,
        agent_depth: int = 0,
        is_sidechain: bool = False,
    ) -> None:
        self._sink = sink
        self._trace_id = trace_id
        self._session_id = session_id
        self._project_hash = project_hash
        self._config = config
        self._redactor = redactor or Redactor(config)
        self._agent_depth = agent_depth
        self._is_sidechain = is_sidechain

        # turn_index for content events is best-effort: the authoritative value
        # arrives with each turn_metric (emitted at turn end).  Content emitted
        # before the first metric of a turn may lag by one (documented v1
        # approximation); turn_metric records are always exact.
        self._current_turn = 1
        self._tool_uses_total = 0

    # -- envelope helper ---------------------------------------------------

    def _base(self) -> dict[str, Any]:
        return {
            "trace_id": self._trace_id,
            "session_id": self._session_id,
            "project_hash": self._project_hash,
            "turn_index": self._current_turn,
            "agent_depth": self._agent_depth,
            "is_sidechain": self._is_sidechain,
        }

    async def _emit(self, record: Any) -> None:
        # A logging fault must never reach the conversation.
        try:
            await self._sink.emit(record)
        except Exception:  # pragma: no cover - sink already swallows
            pass

    # -- lifecycle records -------------------------------------------------

    async def record_session_start(
        self, agent_config: "AgentConfig", *, resumed: bool
    ) -> None:
        await self._emit(
            SessionStartRecord(
                **self._base(),
                model=agent_config.model,
                permission_mode=agent_config.permission_mode,
                workspace_root=agent_config.workspace_root,
                resumed=resumed,
            )
        )

    async def record_user_prompt(self, prompt: str) -> None:
        if not self._config.record_content:
            return
        await self._emit(
            UserPromptRecord(**self._base(), text=self._redactor.text(prompt))
        )

    # -- per-message dispatch ---------------------------------------------

    async def record_message(self, message: AgentMessage) -> None:
        kind = message.type
        if kind == "assistant_text":
            if self._config.record_content:
                await self._emit(
                    AssistantTextRecord(
                        **self._base(), text=self._redactor.text(message.content)
                    )
                )
        elif kind == "tool_use":
            self._tool_uses_total += 1
            if self._config.record_content:
                tool_input: dict[str, Any] = {}
                if self._config.record_tool_io:
                    tool_input = self._redactor.tool_input(message.tool_input)
                await self._emit(
                    ToolUseRecord(
                        **self._base(),
                        tool_use_id=message.tool_use_id,
                        tool_name=message.tool_name,
                        tool_input=tool_input,
                    )
                )
        elif kind == "tool_result":
            if self._config.record_content:
                content: Any = None
                if self._config.record_tool_io:
                    content = self._redactor.tool_result(message.content)
                await self._emit(
                    ToolResultRecord(
                        **self._base(),
                        tool_use_id=message.tool_use_id,
                        tool_name=message.tool_name,
                        content=content,
                        is_error=message.is_error,
                    )
                )
        elif kind == "error":
            await self._emit(
                ErrorRecord(
                    **self._base(),
                    message=message.message,
                    error_type=message.error_type,
                )
            )
        elif kind == "done":
            await self._emit(
                SessionEndRecord(
                    **self._base(),
                    stop_reason=message.stop_reason,
                    total_tokens=message.total_tokens,
                    tool_uses_total=self._tool_uses_total,
                    error_detail=message.error_detail,
                )
            )
        # "compact" (compression notification) is intentionally ignored in v1.

    # -- turn metric (from TurnTracer) ------------------------------------

    async def emit_turn_metric(self, payload: dict[str, Any]) -> None:
        """Build a :class:`TurnMetricRecord` from ``_TurnRecord.to_dict()``.

        ``payload`` is the dict produced by ``logging/tracer.py`` — single
        source of truth for the metric field set.  ``trace_id`` is stripped
        from it (supplied by the envelope) and the rest is passed through.
        """
        turn_index = payload.get("turn_index")
        if isinstance(turn_index, int) and turn_index > 0:
            self._current_turn = turn_index

        # ``trace_id`` and ``turn_index`` are supplied by the envelope (_base);
        # exclude them from the payload pass-through to avoid duplicate kwargs.
        fields = {
            k: v for k, v in payload.items() if k not in ("trace_id", "turn_index")
        }
        await self._emit(TurnMetricRecord(**self._base(), **fields))
