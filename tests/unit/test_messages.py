from __future__ import annotations

import pydantic
import pytest
from pydantic import TypeAdapter

from agent_core.models import (
    AgentMessage,
    AgentResult,
    AssistantText,
    DoneMessage,
    ErrorMessage,
    ToolResult,
    ToolUse,
)


@pytest.mark.message
class TestAgentResult:
    def test_end_turn_result(self) -> None:
        result = AgentResult(
            content="Task completed.",
            total_tokens=1500,
            tool_uses=3,
            stop_reason="end_turn",
        )
        assert result.content == "Task completed."
        assert result.total_tokens == 1500
        assert result.tool_uses == 3
        assert result.stop_reason == "end_turn"
        assert result.error_detail is None

    def test_error_result_with_detail(self) -> None:
        result = AgentResult(
            content="",
            total_tokens=200,
            tool_uses=0,
            stop_reason="error",
            error_detail="LLM API connection timeout",
        )
        assert result.stop_reason == "error"
        assert result.error_detail == "LLM API connection timeout"

    def test_max_turns_result(self) -> None:
        result = AgentResult(
            content="Partial output...",
            total_tokens=5000,
            tool_uses=20,
            stop_reason="max_turns",
        )
        assert result.stop_reason == "max_turns"

    def test_permission_denied_result(self) -> None:
        result = AgentResult(
            content="",
            total_tokens=100,
            tool_uses=0,
            stop_reason="permission_denied",
        )
        assert result.stop_reason == "permission_denied"

    def test_invalid_stop_reason_raises_validation_error(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AgentResult(
                content="",
                total_tokens=0,
                tool_uses=0,
                stop_reason="invalid_stop_reason",  # type: ignore[arg-type]
            )


@pytest.mark.message
class TestAgentMessage:
    def test_assistant_text_message(self) -> None:
        msg: AgentMessage = AssistantText(content="Hello!")
        assert msg.type == "assistant_text"
        assert msg.content == "Hello!"

    def test_tool_use_message(self) -> None:
        msg: AgentMessage = ToolUse(
            tool_use_id="tu_001",
            tool_name="Read",
            tool_input={"path": "/tmp/test.txt"},
        )
        assert msg.type == "tool_use"
        assert msg.tool_use_id == "tu_001"
        assert msg.tool_name == "Read"

    def test_tool_result_message(self) -> None:
        msg: AgentMessage = ToolResult(
            tool_use_id="tu_001",
            tool_name="Read",
            content="file contents here",
        )
        assert msg.type == "tool_result"
        assert msg.tool_use_id == "tu_001"
        assert msg.content == "file contents here"
        assert msg.is_error is False

    def test_tool_result_is_error_flag_defaults_false(self) -> None:
        result = ToolResult(
            tool_use_id="tu_x",
            tool_name="Frob",
            content="ok",
        )
        assert result.is_error is False

    def test_tool_result_is_error_flag_explicit(self) -> None:
        result = ToolResult(
            tool_use_id="tu_err",
            tool_name="Frob",
            content="permission denied",
            is_error=True,
        )
        assert result.is_error is True

    def test_tool_result_content_can_be_dict(self) -> None:
        msg: AgentMessage = ToolResult(
            tool_use_id="tu_002",
            tool_name="Glob",
            content={"matches": ["a.py", "b.py"], "count": 2},
        )
        assert isinstance(msg.content, dict)
        assert msg.content["count"] == 2

    def test_error_message(self) -> None:
        msg: AgentMessage = ErrorMessage(message="Something went wrong")
        assert msg.type == "error"
        assert msg.message == "Something went wrong"
        assert msg.error_type is None

    def test_error_message_with_error_type(self) -> None:
        msg: AgentMessage = ErrorMessage(
            message="rate limited",
            error_type="rate_limit",
        )
        assert msg.message == "rate limited"
        assert msg.error_type == "rate_limit"

    def test_done_message_fields(self) -> None:
        done = DoneMessage(
            stop_reason="end_turn",
            total_tokens=1234,
        )
        assert done.type == "done"
        assert done.stop_reason == "end_turn"
        assert done.total_tokens == 1234
        assert done.error_detail is None

    def test_done_message_with_error(self) -> None:
        done = DoneMessage(
            stop_reason="error",
            total_tokens=500,
            error_detail="connection lost",
        )
        assert done.stop_reason == "error"
        assert done.total_tokens == 500
        assert done.error_detail == "connection lost"

    def test_done_message_invalid_stop_reason_raises(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            DoneMessage(stop_reason="bogus")  # type: ignore[arg-type]

    def test_discriminator_parses_assistant_text(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        parsed = adapter.validate_python({"type": "assistant_text", "content": "hello"})
        assert isinstance(parsed, AssistantText)

    def test_discriminator_parses_tool_use(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        parsed = adapter.validate_python({
            "type": "tool_use",
            "tool_use_id": "tu_01",
            "tool_name": "Read",
            "tool_input": {},
        })
        assert isinstance(parsed, ToolUse)
        assert parsed.tool_use_id == "tu_01"

    def test_discriminator_parses_tool_result(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        parsed = adapter.validate_python({
            "type": "tool_result",
            "tool_use_id": "tu_01",
            "tool_name": "Read",
            "content": "done",
        })
        assert isinstance(parsed, ToolResult)
        assert parsed.is_error is False

    def test_discriminator_parses_error_message(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        parsed = adapter.validate_python({
            "type": "error",
            "message": "boom",
            "error_type": "timeout",
        })
        assert isinstance(parsed, ErrorMessage)
        assert parsed.error_type == "timeout"

    def test_discriminator_parses_done_message(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        parsed = adapter.validate_python({
            "type": "done",
            "stop_reason": "max_turns",
            "total_tokens": 999,
        })
        assert isinstance(parsed, DoneMessage)
        assert parsed.stop_reason == "max_turns"
        assert parsed.total_tokens == 999

    def test_discriminator_rejects_unknown_type(self) -> None:
        adapter = TypeAdapter(AgentMessage)
        with pytest.raises(pydantic.ValidationError):
            adapter.validate_python({"type": "unknown_type", "content": "x"})
