"""
Unit tests for ``SkillTool`` (Item 9).

Covers input validation, registry resolution, required-parameter checks,
inline/fork dispatch, and the destructive-tool safety flag.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.skills.definition import SkillDefinition, SkillParameter
from agent_core.skills.registry import SkillRegistry
from agent_core.skills.tool import SkillTool, _set_skill_registry
from agent_core.tools.base import ToolCallResult, ToolUseContext


def _ctx(registry: SkillRegistry | None = None) -> ToolUseContext:
    ctx = ToolUseContext(agent_id="t-skill")
    if registry is not None:
        _set_skill_registry(ctx, registry)
    return ctx


def _run(coro):
    return asyncio.run(coro)


# ============================================================================
# Static contract
# ============================================================================


class TestSkillToolContract:
    def test_name_and_flags(self) -> None:
        assert SkillTool.name == "Skill"
        # Must be destructive: skills spawn sub-agents that may run Write/Bash,
        # so the engine's non-destructive auto-allow must NOT apply.
        assert SkillTool.is_destructive is True
        assert SkillTool.is_concurrency_safe is False

    def test_validate_input(self) -> None:
        tool = SkillTool()
        parsed = tool.validate_input({"skill": "x", "arguments": {"a": "b"}})
        assert parsed.skill == "x"
        assert parsed.arguments == {"a": "b"}
        assert parsed.mode is None

    def test_input_schema_has_skill_field(self) -> None:
        schema = SkillTool.input_schema()
        assert "skill" in schema["properties"]


# ============================================================================
# Resolution / error paths
# ============================================================================


class TestSkillToolErrors:
    def test_no_registry_attached(self) -> None:
        tool = SkillTool()
        result = _run(tool.call(tool.validate_input({"skill": "x"}), _ctx()))
        assert result.is_error
        assert "not available" in result.content

    def test_unknown_skill(self) -> None:
        reg = SkillRegistry()
        tool = SkillTool()
        result = _run(
            tool.call(tool.validate_input({"skill": "nope"}), _ctx(reg))
        )
        assert result.is_error
        assert "Unknown skill" in result.content

    def test_missing_required_parameter(self) -> None:
        reg = SkillRegistry()
        reg.register(
            SkillDefinition(
                name="needs-arg",
                prompt="Use {{ x }}",
                parameters={"x": SkillParameter(required=True)},
            )
        )
        tool = SkillTool()
        result = _run(
            tool.call(tool.validate_input({"skill": "needs-arg"}), _ctx(reg))
        )
        assert result.is_error
        assert "Missing required" in result.content


# ============================================================================
# Dispatch (inline / fork) — executors are patched
# ============================================================================


class TestSkillToolDispatch:
    def _registry_with(self, **kwargs) -> SkillRegistry:
        reg = SkillRegistry()
        reg.register(SkillDefinition(name="s", prompt="do it", **kwargs))
        return reg

    def test_inline_default(self) -> None:
        reg = self._registry_with(execution_mode="inline")
        tool = SkillTool()
        sentinel = ToolCallResult(content="inline-ran")
        with patch(
            "agent_core.skills.executor.execute_skill_inline",
            new=AsyncMock(return_value=sentinel),
        ) as inline_mock, patch(
            "agent_core.skills.executor.execute_skill_fork",
            new=AsyncMock(),
        ) as fork_mock:
            result = _run(
                tool.call(tool.validate_input({"skill": "s"}), _ctx(reg))
            )
        assert result.content == "inline-ran"
        inline_mock.assert_awaited_once()
        fork_mock.assert_not_awaited()

    def test_fork_when_definition_says_fork(self) -> None:
        reg = self._registry_with(execution_mode="fork")
        tool = SkillTool()
        sentinel = ToolCallResult(content="fork-ran")
        with patch(
            "agent_core.skills.executor.execute_skill_fork",
            new=AsyncMock(return_value=sentinel),
        ) as fork_mock, patch(
            "agent_core.skills.executor.execute_skill_inline",
            new=AsyncMock(),
        ) as inline_mock:
            result = _run(
                tool.call(tool.validate_input({"skill": "s"}), _ctx(reg))
            )
        assert result.content == "fork-ran"
        fork_mock.assert_awaited_once()
        inline_mock.assert_not_awaited()

    def test_input_mode_overrides_definition(self) -> None:
        reg = self._registry_with(execution_mode="inline")
        tool = SkillTool()
        sentinel = ToolCallResult(content="forced-fork")
        with patch(
            "agent_core.skills.executor.execute_skill_fork",
            new=AsyncMock(return_value=sentinel),
        ) as fork_mock, patch(
            "agent_core.skills.executor.execute_skill_inline",
            new=AsyncMock(),
        ) as inline_mock:
            result = _run(
                tool.call(
                    tool.validate_input({"skill": "s", "mode": "fork"}),
                    _ctx(reg),
                )
            )
        assert result.content == "forced-fork"
        fork_mock.assert_awaited_once()
        inline_mock.assert_not_awaited()

    def test_executor_exception_becomes_error_result(self) -> None:
        reg = self._registry_with(execution_mode="inline")
        tool = SkillTool()
        with patch(
            "agent_core.skills.executor.execute_skill_inline",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = _run(
                tool.call(tool.validate_input({"skill": "s"}), _ctx(reg))
            )
        assert result.is_error
        assert "boom" in result.content
