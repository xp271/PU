from __future__ import annotations

from typing import Any

import pydantic
import pytest
from pydantic import TypeAdapter

from agent_core.agent import Agent
from agent_core.models import (
    AgentConfig,
    AgentMessage,
    AgentResult,
    AssistantText,
    ClaudeModel,
    DoneMessage,
    ErrorMessage,
    ToolResult,
    ToolUse,
)


class TestClaudeModel:
    def test_known_values_contains_all_members(self) -> None:
        values = ClaudeModel.known_values()
        assert "claude-sonnet-4-5" in values
        assert "claude-opus-4-7" in values
        assert "claude-haiku-4-5" in values

    def test_member_value_matches_string(self) -> None:
        assert ClaudeModel.SONNET_4_5.value == "claude-sonnet-4-5"
        assert ClaudeModel.OPUS_4_7.value == "claude-opus-4-7"
        assert ClaudeModel.HAIKU_4_5.value == "claude-haiku-4-5"


class TestAgentConfig:
    def test_default_config_has_expected_values(self) -> None:
        config = AgentConfig()
        assert config.model == "claude-sonnet-4-5"
        assert config.max_turns == 20
        assert config.permission_mode == "ask"
        assert config.tools == ["*"]
        assert config.mcp_servers == {}
        assert config.skills_dir is None
        assert config.hooks == []
        assert config.allow_unknown_model is False

    def test_custom_config_accepts_valid_values(self) -> None:
        mcp = {"test_server": {"command": "echo", "args": []}}
        config = AgentConfig(
            model="claude-haiku-4-5",
            max_turns=30,
            permission_mode="bypassPermissions",
            tools=["Read", "Glob"],
            mcp_servers=mcp,
            skills_dir="/tmp/skills",
        )
        assert config.model == "claude-haiku-4-5"
        assert config.max_turns == 30
        assert config.permission_mode == "bypassPermissions"
        assert config.tools == ["Read", "Glob"]
        assert config.mcp_servers == mcp
        assert config.skills_dir == "/tmp/skills"

    def test_invalid_model_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            AgentConfig(model="unknown-model-99")

    def test_allow_unknown_model_suppresses_validation_error(self) -> None:
        with pytest.warns(UserWarning, match="is not a known built-in model"):
            config = AgentConfig(
                model="custom-proxy-model-v2",
                allow_unknown_model=True,
            )
        assert config.model == "custom-proxy-model-v2"

    def test_allow_unknown_model_still_requires_explicit_opt_in(self) -> None:
        with pytest.raises(ValueError, match="Set allow_unknown_model=True"):
            AgentConfig(model="unlisted-model")

    def test_invalid_permission_mode_raises_validation_error(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AgentConfig(permission_mode="invalid_mode")  # type: ignore[arg-type]

    def test_empty_tools_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="tools list must not be empty"):
            AgentConfig(tools=[])

    def test_max_turns_zero_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="max_turns must be >= 1"):
            AgentConfig(max_turns=0)

    def test_max_turns_exceeds_limit_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="max_turns must be <= 500"):
            AgentConfig(max_turns=501)

    def test_all_permission_modes_accepted(self) -> None:
        for mode in ("bypassPermissions", "acceptEdits", "ask"):
            config = AgentConfig(permission_mode=mode)  # type: ignore[arg-type]
            assert config.permission_mode == mode


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


class TestAgent:
    def test_init_with_default_config(self) -> None:
        agent = Agent(AgentConfig())
        assert agent._config.model == "claude-sonnet-4-5"

    def test_init_with_custom_config(self) -> None:
        config = AgentConfig(model="claude-haiku-4-5", max_turns=5)
        agent = Agent(config)
        assert agent._config.model == "claude-haiku-4-5"
        assert agent._config.max_turns == 5

    @pytest.mark.asyncio
    async def test_run_raises_not_implemented_error(self) -> None:
        agent = Agent(AgentConfig())
        with pytest.raises(NotImplementedError):
            await agent.run("test prompt")

    @pytest.mark.asyncio
    async def test_stream_raises_not_implemented_error(self) -> None:
        agent = Agent(AgentConfig())
        with pytest.raises(NotImplementedError):
            async for _ in agent.stream("test prompt"):
                pass

    @pytest.mark.asyncio
    async def test_on_permission_check_registers_callback(self) -> None:
        agent = Agent(AgentConfig())

        called_with: list[tuple[str, dict[str, Any]]] = []

        async def my_callback(tool_name: str, tool_input: dict[str, Any]) -> bool:
            called_with.append((tool_name, tool_input))
            return True

        agent.on_permission_check(my_callback)
        assert agent._permission_callback is my_callback

        result = await agent._permission_callback("Read", {"path": "/tmp/f.txt"})
        assert result is True
        assert called_with == [("Read", {"path": "/tmp/f.txt"})]
