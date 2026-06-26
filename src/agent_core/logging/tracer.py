"""
Per-turn structured logging for the agent query loop.

Consumes the ``InternalEvent`` stream produced by a ``BaseClient``, collects
turn-level metrics, and emits a single structured log record via Python's
standard ``logging`` module when the turn ends.

The tracer is transparent — it re-yields every event so that the ``query()``
loop can process them without knowing the tracer exists.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from ..llm.events import InternalEvent

_logger = logging.getLogger("agent_core.turn")


# ---------------------------------------------------------------------------
# Internal turn record
# ---------------------------------------------------------------------------


class _TurnRecord:
    """Accumulates turn-level metrics from ``InternalEvent`` items."""

    __slots__ = (
        "trace_id",
        "turn_index",
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "tool_uses",
        "tool_names",
        "error_type",
        "start_at",
        "finished",
    )

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.turn_index = -1
        self.provider = ""
        self.model = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None
        self.tool_uses = 0
        self.tool_names: set[str] = set()
        self.error_type: str | None = None
        self.start_at = 0.0  # populated on StreamStart
        self.finished = False

    def consume(self, event: InternalEvent) -> None:
        match event.type:
            case "stream_start":
                self.turn_index = event.turn_index  # type: ignore[attr-defined]
                self.provider = event.provider  # type: ignore[attr-defined]
                self.model = event.model  # type: ignore[attr-defined]
                self.start_at = time.monotonic()
            case "token_counts":
                self.input_tokens = event.input_tokens  # type: ignore[attr-defined]
                self.output_tokens = event.output_tokens  # type: ignore[attr-defined]
                self.cache_read_tokens = event.cache_read_tokens  # type: ignore[attr-defined]
                self.cache_write_tokens = event.cache_write_tokens  # type: ignore[attr-defined]
            case "tool_use":
                self.tool_uses += 1
                self.tool_names.add(event.tool_name)  # type: ignore[attr-defined]
            case "error":
                self.error_type = event.error_type  # type: ignore[attr-defined]

    def finalize(self) -> None:
        self.finished = True

    def to_dict(self) -> dict[str, Any]:
        """Return a dict suitable for ``logging.Logger.info(..., extra=...)``."""
        latency_ms: float | None = None
        if self.start_at > 0:
            latency_ms = round((time.monotonic() - self.start_at) * 1000, 1)

        record: dict[str, Any] = {
            "trace_id": self.trace_id,
            "turn_index": self.turn_index,
            "provider": self.provider,
            "model": self.model,
            "status": "error" if self.error_type else "success",
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_uses": self.tool_uses,
            "tool_names": sorted(self.tool_names),
            "latency_ms": latency_ms,
        }
        if self.cache_read_tokens is not None:
            record["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens is not None:
            record["cache_write_tokens"] = self.cache_write_tokens
        if self.error_type is not None:
            record["error_type"] = self.error_type
        return record


# ---------------------------------------------------------------------------
# Public tracer
# ---------------------------------------------------------------------------


class TurnTracer:
    """Transparent ``InternalEvent`` stream wrapper that logs turn-level metrics.

    Usage in ``query()``::

        tracer = TurnTracer(trace_id=self._trace_id)
        async for event in tracer.wrap(client.stream_messages(messages, tools)):
            ...  # process event normally — tracer already collected metrics

    One ``INFO``-level log record is emitted per turn when the stream ends.
    """

    __slots__ = ("_trace_id", "_recorder")

    def __init__(self, trace_id: str, recorder: Any = None) -> None:
        self._trace_id = trace_id
        # Optional ``ConversationRecorder`` for the data/observability log
        # layer (Item 12).  When None, only the standard logging record is
        # emitted — behaviour identical to before.
        self._recorder = recorder

    async def wrap(
        self,
        stream: AsyncGenerator[InternalEvent, None],
    ) -> AsyncGenerator[InternalEvent, None]:
        turn = _TurnRecord(trace_id=self._trace_id)
        try:
            async for event in stream:
                turn.consume(event)
                yield event
        finally:
            turn.finalize()
            payload = turn.to_dict()
            _logger.info("turn_complete", extra=payload)
            if self._recorder is not None:
                # Single source of truth: the metric field set comes straight
                # from ``to_dict()``.  Never let a logging fault propagate.
                try:
                    await self._recorder.emit_turn_metric(payload)
                except Exception:  # pragma: no cover - recorder already guards
                    pass
