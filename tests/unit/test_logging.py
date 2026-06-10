from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agent_core.llm.events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from agent_core.logging import TurnTracer


async def _mock_stream(
    *events: InternalEvent,
) -> AsyncGenerator[InternalEvent, None]:
    for event in events:
        yield event


@pytest.mark.agent
class TestTurnTracer:
    """Tests for the structured logging tracer."""

    @pytest.mark.asyncio
    async def test_events_pass_through_transparently(self) -> None:
        """Events should be re-yielded unchanged by the tracer."""
        tracer = TurnTracer(trace_id="test-id-001")
        original = [StreamStart(turn_index=0, provider="test", model="test/v1")]
        collected: list[InternalEvent] = []
        async for event in tracer.wrap(_mock_stream(*original)):
            collected.append(event)
        assert collected == original

    @pytest.mark.asyncio
    async def test_logs_turn_complete_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """A successful turn should log with status='success'."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-abc")
        events: list[InternalEvent] = [
            StreamStart(turn_index=0, provider="anthropic", model="claude-sonnet-4-5"),
            AssistantText(content="Hello"),
            TokenCounts(input_tokens=100, output_tokens=50),
            StopEvent(stop_reason="end_turn"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "INFO"
        # extra fields are attached to the LogRecord directly
        assert record.trace_id == "trace-abc"  # type: ignore[attr-defined]
        assert record.turn_index == 0  # type: ignore[attr-defined]
        assert record.provider == "anthropic"  # type: ignore[attr-defined]
        assert record.model == "claude-sonnet-4-5"  # type: ignore[attr-defined]
        assert record.status == "success"  # type: ignore[attr-defined]
        assert record.input_tokens == 100  # type: ignore[attr-defined]
        assert record.output_tokens == 50  # type: ignore[attr-defined]
        assert record.tool_uses == 0  # type: ignore[attr-defined]
        assert record.tool_names == []  # type: ignore[attr-defined]
        assert record.latency_ms is not None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_logs_error_turn(self, caplog: pytest.LogCaptureFixture) -> None:
        """A turn with an error should log status='error' with error_type."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-err")
        events: list[InternalEvent] = [
            StreamStart(turn_index=1, provider="openai", model="gpt-4o"),
            ErrorMessage(message="rate limited", error_type="rate_limit"),
            StopEvent(stop_reason="error"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.status == "error"  # type: ignore[attr-defined]
        assert record.error_type == "rate_limit"  # type: ignore[attr-defined]
        assert record.provider == "openai"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_counts_tool_uses_and_names(self, caplog: pytest.LogCaptureFixture) -> None:
        """Tool use events should be counted and tool names collected."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-tools")
        events: list[InternalEvent] = [
            StreamStart(turn_index=0, provider="test", model="test/v1"),
            ToolUse(tool_use_id="tu_1", tool_name="Read", tool_input={"path": "/f"}),
            ToolUse(tool_use_id="tu_2", tool_name="Read", tool_input={"path": "/g"}),
            ToolUse(tool_use_id="tu_3", tool_name="Grep", tool_input={"pattern": "x"}),
            TokenCounts(input_tokens=200, output_tokens=80),
            StopEvent(stop_reason="end_turn"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.tool_uses == 3  # type: ignore[attr-defined]
        assert record.tool_names == ["Grep", "Read"]  # sorted, deduped  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_logs_cache_tokens_when_present(self, caplog: pytest.LogCaptureFixture) -> None:
        """Anthropic cache tokens should appear in the log record."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-cache")
        events: list[InternalEvent] = [
            StreamStart(turn_index=0, provider="anthropic", model="claude-opus-4-7"),
            TokenCounts(
                input_tokens=500,
                output_tokens=100,
                cache_read_tokens=400,
                cache_write_tokens=50,
            ),
            StopEvent(stop_reason="end_turn"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.cache_read_tokens == 400  # type: ignore[attr-defined]
        assert record.cache_write_tokens == 50  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_omits_cache_tokens_when_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """When cache tokens are None (e.g. OpenAI), they should not appear."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-nocache")
        events: list[InternalEvent] = [
            StreamStart(turn_index=0, provider="openai", model="gpt-4o"),
            TokenCounts(input_tokens=100, output_tokens=50),
            StopEvent(stop_reason="end_turn"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record_dict = caplog.records[0].__dict__
        assert "cache_read_tokens" not in record_dict
        assert "cache_write_tokens" not in record_dict

    @pytest.mark.asyncio
    async def test_latency_measured(self, caplog: pytest.LogCaptureFixture) -> None:
        """Latency should be a positive number of milliseconds."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-latency")
        events: list[InternalEvent] = [
            StreamStart(turn_index=0, provider="test", model="test/v1"),
            TokenCounts(input_tokens=10, output_tokens=5),
            StopEvent(stop_reason="end_turn"),
        ]

        async for _ in tracer.wrap(_mock_stream(*events)):
            pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert isinstance(record.latency_ms, float)  # type: ignore[attr-defined]
        assert record.latency_ms >= 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_logs_even_when_stream_aborted(self, caplog: pytest.LogCaptureFixture) -> None:
        """When the stream is interrupted, the tracer should still log."""
        caplog.set_level(logging.INFO, logger="agent_core.turn")

        tracer = TurnTracer(trace_id="trace-abort")

        async def broken_stream() -> AsyncGenerator[InternalEvent, None]:
            yield StreamStart(turn_index=0, provider="test", model="test/v1")
            raise RuntimeError("connection lost")
            yield StopEvent(stop_reason="end_turn")  # unreachable

        with pytest.raises(RuntimeError, match="connection lost"):
            async for _ in tracer.wrap(broken_stream()):
                pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.trace_id == "trace-abort"  # type: ignore[attr-defined]
        assert record.provider == "test"  # type: ignore[attr-defined]
