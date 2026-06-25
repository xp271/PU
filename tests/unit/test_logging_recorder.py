"""Unit tests for ConversationRecorder (Item 12)."""

from __future__ import annotations

import pytest

from agent_core.logging.config import LoggingConfig
from agent_core.logging.recorder import ConversationRecorder
from agent_core.logging.sink import LogSink
from agent_core.models import (
    AssistantText,
    DoneMessage,
    ErrorMessage,
    ToolResult,
    ToolUse,
)


class _MemorySink(LogSink):
    def __init__(self) -> None:
        self.records: list = []
        self._seq = 0

    async def emit(self, record) -> None:
        record.seq = self._seq
        self._seq += 1
        self.records.append(record)

    async def aclose(self) -> None:
        return None

    @property
    def dropped(self) -> int:
        return 0


class _Cfg:
    model = "anthropic/claude-sonnet-4-5"
    permission_mode = "ask"
    workspace_root = None


def _recorder(sink, **cfg):
    return ConversationRecorder(
        sink,
        trace_id="t1",
        session_id="s1",
        project_hash="ph",
        config=LoggingConfig(**cfg),
    )


@pytest.mark.agent
class TestConversationRecorder:
    @pytest.mark.asyncio
    async def test_message_dispatch(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink)
        await rec.record_session_start(_Cfg(), resumed=False)
        await rec.record_user_prompt("hi")
        await rec.record_message(AssistantText(content="yo"))
        await rec.record_message(ToolUse(tool_use_id="u1", tool_name="Bash", tool_input={"command": "ls"}))
        await rec.record_message(ToolResult(tool_use_id="u1", tool_name="Bash", content="out"))
        await rec.record_message(DoneMessage(stop_reason="end_turn", total_tokens=42))
        kinds = [r.kind for r in sink.records]
        assert kinds == [
            "session_start",
            "user_prompt",
            "assistant_text",
            "tool_use",
            "tool_result",
            "session_end",
        ]

    @pytest.mark.asyncio
    async def test_done_carries_token_and_tool_totals(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink)
        await rec.record_message(ToolUse(tool_use_id="u1", tool_name="Bash", tool_input={}))
        await rec.record_message(ToolUse(tool_use_id="u2", tool_name="Read", tool_input={}))
        await rec.record_message(DoneMessage(stop_reason="end_turn", total_tokens=99))
        end = sink.records[-1]
        assert end.total_tokens == 99
        assert end.tool_uses_total == 2

    @pytest.mark.asyncio
    async def test_record_content_false_skips_content(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink, record_content=False)
        await rec.record_user_prompt("hi")
        await rec.record_message(AssistantText(content="yo"))
        await rec.record_message(ToolUse(tool_use_id="u1", tool_name="Bash", tool_input={}))
        await rec.record_message(ErrorMessage(message="boom", error_type="x"))
        await rec.record_message(DoneMessage(stop_reason="error", total_tokens=0))
        kinds = [r.kind for r in sink.records]
        # content (prompt/assistant/tool) skipped; error + done kept
        assert kinds == ["error", "session_end"]

    @pytest.mark.asyncio
    async def test_record_tool_io_false_keeps_metadata_only(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink, record_tool_io=False)
        await rec.record_message(ToolUse(tool_use_id="u1", tool_name="Bash", tool_input={"command": "ls"}))
        await rec.record_message(ToolResult(tool_use_id="u1", tool_name="Bash", content="secret"))
        tu = sink.records[0]
        tr = sink.records[1]
        assert tu.tool_name == "Bash" and tu.tool_input == {}
        assert tr.tool_name == "Bash" and tr.content is None

    @pytest.mark.asyncio
    async def test_user_prompt_redacted(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink, max_text_chars=3)
        await rec.record_user_prompt("abcdefgh")
        assert sink.records[0].text.startswith("abc")
        assert "chars" in sink.records[0].text

    @pytest.mark.asyncio
    async def test_turn_metric_from_payload(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink)
        payload = {
            "trace_id": "t1",
            "turn_index": 2,
            "provider": "anthropic",
            "model": "claude",
            "status": "success",
            "input_tokens": 100,
            "output_tokens": 50,
            "tool_uses": 1,
            "tool_names": ["Bash"],
            "latency_ms": 12.5,
        }
        await rec.emit_turn_metric(payload)
        m = sink.records[0]
        assert m.kind == "turn_metric"
        assert m.turn_index == 2
        assert m.input_tokens == 100
        assert m.provider == "anthropic"
        assert "trace_id" not in m.model_dump(exclude={"trace_id"})  # sanity

    @pytest.mark.asyncio
    async def test_turn_metric_updates_content_turn_index(self) -> None:
        sink = _MemorySink()
        rec = _recorder(sink)
        await rec.emit_turn_metric({"trace_id": "t1", "turn_index": 3, "provider": "x"})
        await rec.record_message(AssistantText(content="next"))
        assert sink.records[-1].turn_index == 3
