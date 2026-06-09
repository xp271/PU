from __future__ import annotations

import asyncio

import pytest

from agent_core.tools import ToolCallResult, ToolUseContext


@pytest.mark.registry
class TestToolUseContext:
    def test_default_values(self) -> None:
        ctx = ToolUseContext()
        assert isinstance(ctx.abort_signal, asyncio.Event)
        assert ctx.read_file_state == {}
        assert ctx.messages == []
        assert ctx.agent_id == ""
        assert ctx.workspace_root is None

    def test_custom_values(self) -> None:
        event = asyncio.Event()
        ctx = ToolUseContext(
            abort_signal=event,
            read_file_state={"f": {}},
            messages=[{"role": "user"}],
            agent_id="abc-123",
            workspace_root="/workspace",
        )
        assert ctx.abort_signal is event
        assert ctx.read_file_state == {"f": {}}
        assert ctx.messages == [{"role": "user"}]
        assert ctx.agent_id == "abc-123"
        assert ctx.workspace_root == "/workspace"


@pytest.mark.registry
class TestToolCallResult:
    def test_defaults(self) -> None:
        result = ToolCallResult(content="ok")
        assert result.content == "ok"
        assert result.is_error is False
        assert result.truncated is False
        assert result.truncation_notice is None
        assert result.metadata is None

    def test_with_metadata(self) -> None:
        result = ToolCallResult(
            content="ok",
            metadata={"lines_added": 3, "lines_removed": 1},
        )
        assert result.metadata == {"lines_added": 3, "lines_removed": 1}

    def test_with_truncation_notice(self) -> None:
        result = ToolCallResult(
            content="partial",
            truncated=True,
            truncation_notice="Showing 50 of 200 results",
        )
        assert result.truncated
        assert result.truncation_notice == "Showing 50 of 200 results"
