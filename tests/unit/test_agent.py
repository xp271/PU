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
