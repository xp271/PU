from __future__ import annotations

from typing import Any

import pytest

from agent_core.agent import Agent
from agent_core.models import AgentConfig


@pytest.mark.agent
class TestAgent:
    def test_init_with_default_config(self) -> None:
        agent = Agent(AgentConfig())
        assert agent._config.model == "anthropic/claude-sonnet-4-5"

    def test_init_with_custom_config(self) -> None:
        config = AgentConfig(model="claude-haiku-4-5", max_turns=5)
        agent = Agent(config)
        assert agent._config.model == "claude-haiku-4-5"
        assert agent._config.max_turns == 5

    @pytest.mark.asyncio
    async def test_run_consumes_stream_and_returns_result(self) -> None:
        """run() calls stream() and aggregates into AgentResult."""
        config = AgentConfig(max_turns=1)
        agent = Agent(config)
        result = await agent.run("Say only the word: hello")
        # Valid stop reasons: end_turn (normal), error (no API key), max_turns
        assert result.stop_reason in ("end_turn", "max_turns", "error")
        assert isinstance(result.content, str)
        assert isinstance(result.total_tokens, int)

    @pytest.mark.asyncio
    async def test_run_aggregates_error_turn(self) -> None:
        """run() surfaces an error turn as stop_reason='error' + error_detail
        (precise assertion, no reliance on a real API key)."""
        from unittest.mock import patch

        from tests.integration.mock_model import MockClient, make_error_turn

        client = MockClient(make_error_turn("overloaded"))
        config = AgentConfig(max_turns=2)
        with patch("agent_core.query.loop.get_client", lambda *_: client):
            result = await Agent(config).run("trigger an error")

        assert result.stop_reason == "error"
        assert result.error_detail is not None
        assert "overloaded" in result.error_detail

    @pytest.mark.asyncio
    async def test_stream_yields_messages(self) -> None:
        """stream() delegates to query() and yields AgentMessage items."""
        config = AgentConfig(max_turns=1)
        agent = Agent(config)
        messages: list[Any] = []
        async for msg in agent.stream("Say only the word: hello"):
            messages.append(msg)
        # At minimum should yield a DoneMessage.
        types = [m.type for m in messages]  # type: ignore[attr-defined]
        assert "done" in types

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
