from __future__ import annotations

import asyncio
import os

import pytest

from agent_core.tools import BashTool, ToolUseContext, get_tool


@pytest.mark.bash
@pytest.mark.slow
class TestBashTool:
    def tool(self) -> BashTool:
        return get_tool("Bash")  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_echo_command(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": "echo hello"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "hello" in str(result.content)

    @pytest.mark.asyncio
    async def test_command_failure(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": "nonexistent_command_xyz 2>&1 || exit 1"}),
            ToolUseContext(),
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        tool = self.tool()
        if os.name == "nt":
            cmd = "ping -n 30 127.0.0.1 > NUL"
        else:
            cmd = "sleep 10"
        result = await tool.call(
            tool.validate_input({"command": cmd, "timeout": 1}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "timed out" in str(result.content)

    @pytest.mark.asyncio
    async def test_abort_signal_kills_process(self) -> None:
        tool = self.tool()
        event = asyncio.Event()
        event.set()
        ctx = ToolUseContext(abort_signal=event)
        if os.name == "nt":
            cmd = "ping -n 30 127.0.0.1 > NUL"
        else:
            cmd = "sleep 10"
        result = await tool.call(
            tool.validate_input({"command": cmd, "timeout": 30}),
            ctx,
        )
        assert result.is_error
        assert "aborted" in str(result.content)

    @pytest.mark.asyncio
    async def test_no_output_command(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "pass"'}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert result.content == "[no output]"

    @pytest.mark.asyncio
    async def test_both_stdout_and_stderr(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "import sys; print(\'out\'); print(\'err\', file=sys.stderr)"'}),
            ToolUseContext(),
        )
        assert not result.is_error
        content = str(result.content)
        assert "out" in content
        assert "[stderr]" in content
        assert "err" in content

    @pytest.mark.asyncio
    async def test_exit_code_in_output(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "import sys; sys.exit(42)"'}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "(exit code: 42)" in str(result.content)
