"""Unit tests for AgentTool."""

import asyncio

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.resolve import MAX_AGENT_DEPTH
from agent_core.agents.tool import AgentInput, AgentTool
from agent_core.models import AgentConfig
from agent_core.tools.base import ToolUseContext


class TestAgentInput:
    def test_minimal_fields(self):
        inp = AgentInput.model_validate({"description": "test", "prompt": "do"})
        assert inp.description == "test"
        assert inp.prompt == "do"
        assert inp.subagent_type == "GeneralPurpose"
        assert inp.model is None
        assert inp.run_in_background is False

    def test_missing_required(self):
        with pytest.raises(Exception):
            AgentInput.model_validate({"description": "test"})


class TestAgentToolProperties:
    def test_name(self):
        assert AgentTool.name == "Agent"

    def test_is_destructive(self):
        assert AgentTool.is_destructive is True

    def test_is_not_read_only(self):
        assert AgentTool.is_read_only is False


class TestAgentToolRouting:
    def test_rejects_unknown_agent_type(self):
        tool = AgentTool()
        context = ToolUseContext(
            parent_config=AgentConfig(),
            parent_trace_id="test",
        )
        inp = AgentInput(description="test", prompt="do", subagent_type="NonExistent")
        result = asyncio.run(tool.call(inp, context))
        assert result.is_error
        assert "Unknown agent type" in str(result.content)

    def test_rejects_missing_parent_config(self):
        tool = AgentTool()
        context = ToolUseContext()  # no parent_config
        inp = AgentInput(description="test", prompt="do")
        result = asyncio.run(tool.call(inp, context))
        assert result.is_error
        assert "parent_config" in str(result.content)

    def test_rejects_at_depth_limit(self):
        tool = AgentTool()
        context = ToolUseContext(
            parent_config=AgentConfig(),
            parent_trace_id="test",
            agent_depth=MAX_AGENT_DEPTH,
        )
        inp = AgentInput(description="test", prompt="do")
        result = asyncio.run(tool.call(inp, context))
        assert result.is_error
        assert "depth" in str(result.content)


class TestAgentToolModelAlias:
    def test_haiku_alias_maps_to_full_model(self):
        from agent_core.agents.tool import _MODEL_ALIASES
        assert _MODEL_ALIASES["haiku"] == "anthropic/claude-haiku-4-5"
        assert _MODEL_ALIASES["sonnet"] == "anthropic/claude-sonnet-4-5"
        assert _MODEL_ALIASES["opus"] == "anthropic/claude-opus-4-7"
