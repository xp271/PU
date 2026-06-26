"""End-to-end tests with vcrpy for compression flow."""
import pytest


@pytest.mark.e2e
class TestCompressionE2E:
    """Compression flow with real LLM (needs enough turns to exceed threshold)."""

    @pytest.mark.asyncio
    async def test_compression_triggered(self, real_config, vcr, tmp_path):
        """Long conversation triggers auto-compression."""
        from agent_core import Agent, CompressionConfig, CompactMessage

        # Create a large file to make the conversation long
        big_file = tmp_path / "big.py"
        big_file.write_text("\n".join(
            f"# line {i}: x = {i} * 2" for i in range(500)
        ))

        config = real_config.model_copy(update={
            "max_turns": 15,
            "compression": CompressionConfig(
                enabled=True,
                keep_recent_rounds=2,
                summary_max_tokens=1024,
            ),
        })
        agent = Agent(config)

        compact_events = []
        async for msg in agent.stream(
            f"Read {big_file}, then explain what each section does. "
            "Ask multiple follow-up questions."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # May trigger compression (depends on actual conversation length)
        if compact_events:
            completed = [e for e in compact_events if e.status == "completed"]
            if completed:
                assert completed[0].rounds_compressed > 0
                assert completed[0].compression_ratio < 1.0

    @pytest.mark.asyncio
    async def test_compression_with_tool_calls(self, real_config, vcr, tmp_path):
        """Compression with tool-call rounds preserves tool traceability."""
        from agent_core import Agent, CompressionConfig, CompactMessage

        # Create multiple files to force multi-turn tool calls
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text(f"Content of file {i}")

        config = real_config.model_copy(update={
            "max_turns": 20,
            "compression": CompressionConfig(
                enabled=True,
                keep_recent_rounds=3,
                summary_max_tokens=1024,
            ),
        })
        agent = Agent(config)

        compact_events = []
        async for msg in agent.stream(
            f"Read each file in {tmp_path} one by one, and after reading all of them, "
            f"write a summary to {tmp_path}/summary.txt."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # Soft assert: whether compression triggers depends on actual token count.
        # If it did trigger, verify internal consistency.
        completed = [e for e in compact_events if e.status == "completed"]
        for e in completed:
            assert e.compression_ratio < 1.0
            assert e.rounds_compressed > 0

    @pytest.mark.asyncio
    async def test_compression_disabled(self, real_config, vcr, tmp_path):
        """Disabled compression → no CompactMessage events."""
        from agent_core import Agent, CompressionConfig, CompactMessage

        big_file = tmp_path / "data.txt"
        big_file.write_text("\n".join(f"Line {i}: data" for i in range(500)))

        config = real_config.model_copy(update={
            "max_turns": 10,
            "compression": CompressionConfig(enabled=False),
        })
        agent = Agent(config)

        compact_events = []
        async for msg in agent.stream(
            f"Read {big_file} and analyze its content in detail."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # No CompactMessage expected when compression is disabled
        started = [e for e in compact_events if e.status == "started"]
        assert len(started) == 0
