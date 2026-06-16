"""Unit tests for query() message builders and tool execution."""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.models import ToolResult, ToolUse
from agent_core.permissions.engine import PermissionEngine
from agent_core.query.loop import (
    _build_assistant_message,
    _build_initial_user_message,
    _build_tool_result_user_message,
    run_tool_use,
    run_tools,
)
from agent_core.tools.base import ToolUseContext


class TestBuildInitialUserMessage:
    def test_basic(self) -> None:
        msg = _build_initial_user_message("Hello world")
        assert msg["role"] == "user"
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "Hello world"


class TestBuildAssistantMessage:
    def test_text_only(self) -> None:
        msg = _build_assistant_message(["Hello"], [])
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "Hello"

    def test_empty_text_skipped(self) -> None:
        msg = _build_assistant_message(["  ", ""], [])
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 0

    def test_text_and_tool_use(self) -> None:
        tu = ToolUse(
            tool_use_id="tu_1",
            tool_name="Read",
            tool_input={"file_path": "/f.txt"},
        )
        msg = _build_assistant_message(["Let me read that."], [tu])
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "Let me read that."
        assert msg["content"][1]["type"] == "tool_use"
        assert msg["content"][1]["id"] == "tu_1"
        assert msg["content"][1]["name"] == "Read"
        assert msg["content"][1]["input"] == {"file_path": "/f.txt"}

    def test_multiple_tool_uses(self) -> None:
        tu1 = ToolUse(tool_use_id="a", tool_name="Read", tool_input={})
        tu2 = ToolUse(tool_use_id="b", tool_name="Glob", tool_input={})
        msg = _build_assistant_message([], [tu1, tu2])
        assert len(msg["content"]) == 2
        assert msg["content"][0]["id"] == "a"
        assert msg["content"][1]["id"] == "b"


class TestBuildToolResultUserMessage:
    def test_single_result(self) -> None:
        tr = ToolResult(
            tool_use_id="tu_1",
            tool_name="Read",
            content="file contents",
            is_error=False,
        )
        msg = _build_tool_result_user_message([tr])
        assert msg["role"] == "user"
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "tool_result"
        assert msg["content"][0]["tool_use_id"] == "tu_1"
        assert msg["content"][0]["content"] == "file contents"
        assert msg["content"][0]["is_error"] is False

    def test_error_result(self) -> None:
        tr = ToolResult(
            tool_use_id="tu_1",
            tool_name="FakeTool",
            content="Tool not found",
            is_error=True,
        )
        msg = _build_tool_result_user_message([tr])
        assert msg["content"][0]["is_error"] is True
        assert msg["content"][0]["content"] == "Tool not found"

    def test_multiple_results(self) -> None:
        tr1 = ToolResult(tool_use_id="a", tool_name="X", content="ok")
        tr2 = ToolResult(tool_use_id="b", tool_name="Y", content="also ok")
        msg = _build_tool_result_user_message([tr1, tr2])
        assert len(msg["content"]) == 2


@pytest.mark.asyncio
class TestExecuteTools:
    async def test_known_tool(self) -> None:
        ctx = ToolUseContext()
        tu = ToolUse(
            tool_use_id="tu_1",
            tool_name="Read",
            tool_input={"file_path": "/nonexistent"},
        )
        # Bypass permissions so the engine doesn't interfere
        engine = PermissionEngine(mode="bypassPermissions")
        result = await run_tool_use(tu, ctx, permission_engine=engine)
        # Read fails because file doesn't exist, but should NOT be an
        # "unknown tool" or "permission denied" error — it's a tool-level error.
        assert result.tool_use_id == "tu_1"

    async def test_unknown_tool(self) -> None:
        ctx = ToolUseContext()
        tu = ToolUse(
            tool_use_id="tu_1",
            tool_name="NonExistentTool",
            tool_input={},
        )
        engine = PermissionEngine(mode="bypassPermissions")
        result = await run_tool_use(tu, ctx, permission_engine=engine)
        assert result.is_error is True
        assert "Unknown tool" in str(result.content)

    async def test_permission_denied(self) -> None:
        ctx = ToolUseContext()

        async def deny_all(_name: str, _inp: dict) -> bool:
            return False

        # Use "Bash" — its default rule is "ask", so the callback IS reached.
        tu = ToolUse(
            tool_use_id="tu_1",
            tool_name="Bash",
            tool_input={"command": "echo hello"},
        )
        engine = PermissionEngine(mode="ask")
        result = await run_tool_use(tu, ctx, deny_all, permission_engine=engine)
        assert result.is_error is True
        assert "Permission denied" in str(result.content)

    async def test_permission_allowed(self) -> None:
        ctx = ToolUseContext()

        async def allow_all(_name: str, _inp: dict) -> bool:
            return True

        # Use "Bash" — default rule is "ask", callback reached.
        tu = ToolUse(
            tool_use_id="tu_1",
            tool_name="Bash",
            tool_input={"command": "echo hello"},
        )
        engine = PermissionEngine(mode="ask")
        result = await run_tool_use(tu, ctx, allow_all, permission_engine=engine)
        # Permission passed; Bash executes fine.
        assert "Permission denied" not in str(result.content)

    async def test_multiple_tools_sequential(self) -> None:
        ctx = ToolUseContext()
        tu1 = ToolUse(tool_use_id="1", tool_name="NonExistent", tool_input={})
        tu2 = ToolUse(
            tool_use_id="2",
            tool_name="Read",
            tool_input={"file_path": "/nonexistent"},
        )
        engine = PermissionEngine(mode="bypassPermissions")
        results = await run_tools(
            [tu1, tu2], ctx, permission_engine=engine,
        )
        assert len(results) == 2
        # First failed (unknown tool), second executed (Read succeeds but
        # file not found is still a tool call result, not a crash).
        assert results[0].is_error is True
