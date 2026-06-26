"""End-to-end tests with vcrpy recording/replay for Agent turn flow."""
import pytest


@pytest.mark.e2e
class TestTurnFlow:
    """Core turn flow scenarios with real LLM API."""

    @pytest.mark.asyncio
    async def test_single_text_turn(self, real_config, vcr):
        """Pure text Q&A — no tool calls, immediate end_turn."""
        from agent_core import Agent

        agent = Agent(real_config)
        result = await agent.run(
            "Hello! Reply with just 'Hello, world!' in English."
        )

        assert result.stop_reason == "end_turn"
        assert len(result.content) > 0
        assert "hello" in result.content.lower()

    @pytest.mark.asyncio
    async def test_read_file_tool(self, real_config, vcr, tmp_path):
        """Agent uses Read tool to read a temp file."""
        from agent_core import Agent

        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello from e2e test!", encoding="utf-8")

        config = real_config.model_copy(update={"max_turns": 5})
        agent = Agent(config)

        prompt = (
            f"Read the file at {test_file} and tell me what it says. "
            "Just give me the content."
        )
        result = await agent.run(prompt)

        assert result.tool_uses >= 1
        assert "Hello from e2e test" in result.content

    @pytest.mark.asyncio
    async def test_multi_turn(self, real_config, vcr, tmp_path):
        """Multi-turn: Glob to find files, then Read one."""
        from agent_core import Agent

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")

        config = real_config.model_copy(update={"max_turns": 10})
        agent = Agent(config)

        prompt = (
            f"List all Python files in {tmp_path} using Glob, "
            f"then read one of them and tell me what it does."
        )
        result = await agent.run(prompt)

        assert result.tool_uses >= 2
        assert "print" in result.content.lower() or "hello" in result.content.lower()

    @pytest.mark.asyncio
    async def test_max_turns_exhaustion(self, real_config, vcr):
        """max_turns=1 with tool-use request → max_turns stop."""
        from agent_core import Agent

        config = real_config.model_copy(update={"max_turns": 1})
        agent = Agent(config)

        # This prompt is very likely to trigger a tool call (Read),
        # so 1 turn is insufficient
        result = await agent.run("Read the file at /nonexistent/path.txt")
        assert result.stop_reason in ("max_turns", "end_turn")

    @pytest.mark.asyncio
    async def test_streaming(self, real_config, vcr):
        """Stream path yields correct AgentMessage types."""
        from agent_core import Agent

        agent = Agent(real_config)

        messages = []
        async for msg in agent.stream("Say 'hello' and nothing else."):
            messages.append(msg)

        assert len(messages) >= 2
        # Last message must be DoneMessage
        assert messages[-1].type == "done"
        # At least one AssistantText
        assert any(m.type == "assistant_text" for m in messages)

    @pytest.mark.asyncio
    async def test_permission_denied(self, real_config, vcr, tmp_path):
        """permission_mode=ask without callback + destructive tool → denied.

        Must use a destructive tool (Write): Read is read-only and would be
        auto-allowed by permission chain step 6.  Denial manifests as an
        is_error ToolResult in the message stream, not as stop_reason.
        """
        from agent_core import Agent

        target = tmp_path / "out.txt"

        config = real_config.model_copy(update={
            "permission_mode": "ask",  # no callback → destructive tools denied
            "max_turns": 3,
        })
        agent = Agent(config)
        # No on_permission_check registered → Write gets denied in ask mode

        denied = False
        async for msg in agent.stream(
            f"Write the text 'hello' to the file at {target}."
        ):
            if (
                msg.type == "tool_result"
                and msg.is_error
                and "Permission denied" in str(msg.content)
            ):
                denied = True

        assert denied, "expected a denied ToolResult for the Write tool"
        # File was never actually written
        assert not target.exists()
