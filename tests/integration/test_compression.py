"""Integration tests for the compression module with MockClient.

Covers:
- compress() end-to-end with a mock LLM (standalone, no loop)
- compress() inside the query() loop (CompactMessage events, fallback to truncation)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent_core.compression.summarizer import (
    CompressionAttempt,
    CompressionError,
    compress,
)
from agent_core.llm.events import (
    AssistantText,
    ErrorMessage,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from agent_core.models import AgentConfig, AgentMessage, CompactMessage, CompressionConfig
from agent_core.query.loop import query
from tests.integration.mock_model import MockClient, make_text_turn


# ============================================================================
# Helpers
# ============================================================================


def _collect(agen) -> list[AgentMessage]:
    async def _drain():
        result: list[AgentMessage] = []
        async for msg in agen:
            result.append(msg)
        return result
    return asyncio.run(_drain())


def _mock_get_client_factory(client):
    def _factory(_model_id, _config):
        return client
    return _factory


def _make_msg(role: str, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "content": list(blocks)}


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use_block(name="Read", tid="tu_001", inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def _tool_result_block(tid="tu_001", content="result", is_error=False):
    return {
        "type": "tool_result", "tool_use_id": tid,
        "content": content, "is_error": is_error,
    }


def _build_6_round_messages() -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for i in range(1, 7):
        msgs.append(_make_msg("user", _text_block(f"question {i}")))
        msgs.append(_make_msg("assistant", _text_block(f"answer {i}")))
    return msgs


# ============================================================================
# compress() standalone — MockClient
# ============================================================================


class TestCompressWithMockClient:
    """Test the compress() function end-to-end using a scriptable MockClient."""

    def test_first_attempt_succeeds(self) -> None:
        summary_text = "This is a valid summary of the conversation, covering key points."
        script = [
            AssistantText(content=summary_text),
            TokenCounts(input_tokens=100, output_tokens=30),
            StopEvent(stop_reason="end_turn"),
        ]
        client = MockClient(script)
        messages = _build_6_round_messages()
        config = CompressionConfig(keep_recent_rounds=3, max_retries=2)

        compressed, summary, attempts = asyncio.run(
            compress(client=client, messages=messages, system=None,
                     model_id=AgentConfig().model_id, config=config)
        )

        assert summary == summary_text
        assert len(attempts) == 1
        assert attempts[0].verification.valid is True
        assert attempts[0].input_tokens_consumed == 100
        assert len(compressed) == 7  # 1 summary + 3 recent×2 msgs
        assert compressed[0]["role"] == "user"
        assert "<conversation_summary>" in compressed[0]["content"][0]["text"]

    def test_retry_then_succeed(self) -> None:
        empty_script = [
            AssistantText(content=""),
            TokenCounts(input_tokens=10, output_tokens=1),
            StopEvent(stop_reason="end_turn"),
        ]
        good_summary = "A proper summary with sufficient length to pass verification."
        good_script = [
            AssistantText(content=good_summary),
            TokenCounts(input_tokens=80, output_tokens=25),
            StopEvent(stop_reason="end_turn"),
        ]

        class MultiScriptClient(MockClient):
            def __init__(self, scripts, **kwargs):
                super().__init__([], **kwargs)
                self._scripts = scripts
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(turn_index=0, provider="mock", model=self.model)
                idx = self._call
                self._call += 1
                for event in self._scripts[idx]:
                    yield event

        client = MultiScriptClient([empty_script, good_script])
        config = CompressionConfig(keep_recent_rounds=3, max_retries=2)

        compressed, summary, attempts = asyncio.run(
            compress(client=client, messages=_build_6_round_messages(),
                     system=None, model_id=AgentConfig().model_id, config=config)
        )

        assert len(attempts) == 2
        assert attempts[0].verification.valid is False
        assert attempts[1].verification.valid is True
        assert summary == good_summary

    def test_all_retries_exhausted(self) -> None:
        empty_script = [
            AssistantText(content=""),
            TokenCounts(input_tokens=10, output_tokens=1),
            StopEvent(stop_reason="end_turn"),
        ]
        client = MockClient(empty_script)
        config = CompressionConfig(keep_recent_rounds=3, max_retries=2)

        with pytest.raises(CompressionError, match="verification failed after 3"):
            asyncio.run(
                compress(client=client, messages=_build_6_round_messages(),
                         system=None, model_id=AgentConfig().model_id, config=config)
            )

    def test_max_retries_zero(self) -> None:
        empty_script = [
            AssistantText(content=""),
            TokenCounts(input_tokens=10, output_tokens=1),
            StopEvent(stop_reason="end_turn"),
        ]
        client = MockClient(empty_script)
        config = CompressionConfig(keep_recent_rounds=3, max_retries=0)

        with pytest.raises(CompressionError, match="verification failed after 1"):
            asyncio.run(
                compress(client=client, messages=_build_6_round_messages(),
                         system=None, model_id=AgentConfig().model_id, config=config)
            )

    def test_llm_call_exception_then_retries(self) -> None:
        class FailingThenOkClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(turn_index=0, provider="mock", model=self.model)
                self._call += 1
                if self._call == 1:
                    raise RuntimeError("simulated network error")
                yield AssistantText(content="Good summary with enough characters to pass all checks.")
                yield TokenCounts(input_tokens=50, output_tokens=20)
                yield StopEvent(stop_reason="end_turn")

        client = FailingThenOkClient()
        config = CompressionConfig(keep_recent_rounds=3, max_retries=2)

        compressed, summary, attempts = asyncio.run(
            compress(client=client, messages=_build_6_round_messages(),
                     system=None, model_id=AgentConfig().model_id, config=config)
        )

        assert len(attempts) == 2
        assert attempts[0].verification.valid is False
        assert "LLM call failed" in attempts[0].verification.errors[0]
        assert attempts[0].error == "simulated network error"
        assert attempts[1].verification.valid is True

    def test_not_enough_rounds_returns_original(self) -> None:
        messages = _build_6_round_messages()[:4]  # 2 rounds
        client = MockClient([])
        config = CompressionConfig(keep_recent_rounds=3)

        compressed, summary, attempts = asyncio.run(
            compress(client=client, messages=messages, system=None,
                     model_id=AgentConfig().model_id, config=config)
        )

        assert compressed == messages
        assert summary == ""
        assert attempts == []

    def test_retry_prompt_includes_previous_errors(self) -> None:
        prompts_seen: list[str] = []

        class CapturingClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, messages, **kwargs):
                for msg in messages:
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            prompts_seen.append(str(block["text"]))
                yield StreamStart(turn_index=0, provider="mock", model=self.model)
                self._call += 1
                if self._call == 1:
                    yield AssistantText(content="")
                    yield TokenCounts(input_tokens=10, output_tokens=1)
                    yield StopEvent(stop_reason="end_turn")
                else:
                    yield AssistantText(content="A good summary that passes because it is long enough.")
                    yield TokenCounts(input_tokens=50, output_tokens=20)
                    yield StopEvent(stop_reason="end_turn")

        client = CapturingClient()
        config = CompressionConfig(keep_recent_rounds=3, max_retries=1)

        asyncio.run(
            compress(client=client, messages=_build_6_round_messages(),
                     system=None, model_id=AgentConfig().model_id, config=config)
        )

        assert len(prompts_seen) == 2
        assert "previous_attempt_feedback" not in prompts_seen[0]
        assert "previous_attempt_feedback" in prompts_seen[1]

    def test_preserves_system_in_llm_call(self) -> None:
        captured_system: list[str | None] = []

        class SysCapturingClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)

            async def stream_messages(self, system=None, **kwargs):
                captured_system.append(system)
                yield StreamStart(turn_index=0, provider="mock", model=self.model)
                yield AssistantText(content="Valid summary text that has enough chars.")
                yield TokenCounts(input_tokens=50, output_tokens=20)
                yield StopEvent(stop_reason="end_turn")

        client = SysCapturingClient()
        config = CompressionConfig(keep_recent_rounds=3)
        asyncio.run(
            compress(client=client, messages=_build_6_round_messages(),
                     system="You are helpful.", model_id=AgentConfig().model_id,
                     config=config)
        )
        assert captured_system == ["You are helpful."]

    def test_empty_messages(self) -> None:
        client = MockClient([])
        config = CompressionConfig(keep_recent_rounds=3)
        compressed, summary, attempts = asyncio.run(
            compress(client=client, messages=[], system=None,
                     model_id=AgentConfig().model_id, config=config)
        )
        assert compressed == []
        assert summary == ""
        assert attempts == []

    def test_llm_error_event_raises(self) -> None:
        """An 'error' InternalEvent from the LLM raises CompressionError immediately."""
        error_script = [
            ErrorMessage(message="overloaded", error_type="overloaded"),
            StopEvent(stop_reason="error"),
        ]
        client = MockClient(error_script)
        config = CompressionConfig(keep_recent_rounds=3, max_retries=1)

        with pytest.raises(CompressionError, match="LLM summarization failed"):
            asyncio.run(
                compress(client=client, messages=_build_6_round_messages(),
                         system=None, model_id=AgentConfig().model_id, config=config)
            )

    def test_custom_prompt_template(self) -> None:
        """Custom prompt_template from config is used."""
        captured_prompt: list[str] = []

        class PromptCapturingClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)

            async def stream_messages(self, messages, **kwargs):
                for msg in messages:
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            captured_prompt.append(str(block["text"]))
                yield StreamStart(turn_index=0, provider="mock", model=self.model)
                yield AssistantText(content="Valid summary that passes verification checks.")
                yield TokenCounts(input_tokens=50, output_tokens=20)
                yield StopEvent(stop_reason="end_turn")

        client = PromptCapturingClient()
        custom = "CUSTOM: {conversation_text} [preview={preview_chars}]"
        config = CompressionConfig(keep_recent_rounds=3, prompt_template=custom)

        asyncio.run(
            compress(client=client, messages=_build_6_round_messages(),
                     system=None, model_id=AgentConfig().model_id, config=config)
        )

        assert len(captured_prompt) == 1
        assert captured_prompt[0].startswith("CUSTOM:")


# ============================================================================
# query() loop integration — compression pathway
# ============================================================================


class TestCompressionInQueryLoop:
    """Test the compression pathway inside the full query() loop.

    Because query() starts with a single user-prompt round, we need at
    least one tool-use turn to accumulate enough rounds for can_compress
    to return True.  These tests use a 2-turn mock: turn 1 returns a
    tool_use (creating round 2), turn 2 triggers compression before the
    LLM call and then returns a text response.
    """

    COMPRESSION_SUMMARY = "Valid summary of earlier rounds that is long enough for verification."

    def _make_two_turn_client(
        self,
        compression_result: list,  # script for the compression LLM call
        main_turn_result: list,    # script for turn 2
    ):
        """Return a MockClient that sequences: compression call, then main turn."""

        class TwoTurnClient(MockClient):
            def __init__(self, comp_script, main_script, **kwargs):
                super().__init__([], **kwargs)
                self._comp = comp_script
                self._main = main_script
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1:
                    for event in self._comp:
                        yield event
                elif self._call == 2:
                    for event in self._main:
                        yield event
                else:
                    yield StopEvent(stop_reason="end_turn")

        return TwoTurnClient(compression_result, main_turn_result)

    def test_compression_succeeds_in_loop(self) -> None:
        """2-turn scenario: tool_use → compression triggered → CompactMessage events."""
        # Turn 1: tool_use (creates round 2)
        turn1_script = [
            AssistantText(content="Let me check."),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/test.txt"}),
            TokenCounts(input_tokens=50, output_tokens=15),
            StopEvent(stop_reason="tool_use"),
        ]
        # Compression call: good summary
        comp_script = [
            AssistantText(content=self.COMPRESSION_SUMMARY),
            TokenCounts(input_tokens=100, output_tokens=30),
            StopEvent(stop_reason="end_turn"),
        ]
        # Turn 2: text response
        turn2_script = [
            AssistantText(content="All done."),
            TokenCounts(input_tokens=50, output_tokens=15),
            StopEvent(stop_reason="end_turn"),
        ]

        # 3-call sequence: turn 1 (tool_use), compression, turn 2 (text)
        class ThreePhaseClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1:
                    for e in turn1_script: yield e
                elif self._call == 2:
                    for e in comp_script: yield e
                else:
                    for e in turn2_script: yield e

        client = ThreePhaseClient()
        config = AgentConfig(
            max_turns=3,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1),
        )

        # Use a long prompt so after turn 1 the estimate exceeds threshold
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(long_prompt, config, trace_id="t_c_loop"))

        types = [m.type for m in msgs]
        assert "compact" in types
        compact_msgs = [m for m in msgs if isinstance(m, CompactMessage)]
        started = [m for m in compact_msgs if m.status == "started"]
        completed = [m for m in compact_msgs if m.status == "completed"]
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].tokens_before > 0
        assert completed[0].summary == self.COMPRESSION_SUMMARY
        assert completed[0].retries_used == 0
        assert completed[0].compression_ratio > 0

    def test_compression_fails_fallsback_to_truncation(self) -> None:
        """Compression retries exhausted → skipped CompactMessage + hard_truncate."""
        # Turn 1: tool_use
        turn1_script = [
            AssistantText(content="ok"),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/t.txt"}),
            TokenCounts(input_tokens=50, output_tokens=15),
            StopEvent(stop_reason="tool_use"),
        ]
        # Compression calls: all empty (verification fails)
        empty_comp = [
            AssistantText(content=""),
            TokenCounts(input_tokens=5, output_tokens=1),
            StopEvent(stop_reason="end_turn"),
        ]
        # Turn 2: text
        turn2_script = [
            AssistantText(content="Final."),
            TokenCounts(input_tokens=50, output_tokens=15),
            StopEvent(stop_reason="end_turn"),
        ]

        class FailCompressionClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1:
                    for e in turn1_script: yield e
                elif self._call <= 4:  # 1 attempt + 2 retries = 3 compression calls
                    for e in empty_comp: yield e
                else:
                    for e in turn2_script: yield e

        client = FailCompressionClient()
        config = AgentConfig(
            max_turns=3,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1, max_retries=2),
        )
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(long_prompt, config, trace_id="t_fail"))

        compact_msgs = [m for m in msgs if isinstance(m, CompactMessage)]
        started = [m for m in compact_msgs if m.status == "started"]
        skipped = [m for m in compact_msgs if m.status == "skipped"]
        completed = [m for m in compact_msgs if m.status == "completed"]
        assert len(started) == 1
        assert len(skipped) == 1
        assert len(completed) == 0
        assert skipped[0].error_detail is not None
        # Final result is still a done message
        assert msgs[-1].type == "done"

    def test_compression_disabled_direct_truncation(self) -> None:
        """enabled=False → no CompactMessage, direct hard_truncate."""
        client = MockClient(make_text_turn("OK"))
        config = AgentConfig(
            max_turns=2,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(enabled=False),
        )
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(long_prompt, config, trace_id="t_off"))

        assert not any(isinstance(m, CompactMessage) for m in msgs)
        assert msgs[-1].type == "done"

    def test_compression_not_enough_rounds_direct_truncation(self) -> None:
        """Only 1 round, keep_recent_rounds=1 → can_compress=False → direct truncation."""
        client = MockClient(make_text_turn("OK"))
        config = AgentConfig(
            max_turns=2,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(enabled=True, keep_recent_rounds=2),
        )
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(long_prompt, config, trace_id="t_few"))

        assert not any(isinstance(m, CompactMessage) for m in msgs)
        assert msgs[-1].type == "done"

    def test_compression_token_costs_accumulated_in_budget(self) -> None:
        """Compression tokens added to total without affecting turn count."""
        # Turn 1: tool_use
        turn1 = [
            AssistantText(content="ok"),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/t.txt"}),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="tool_use"),
        ]
        # Compression
        comp = [
            AssistantText(content=self.COMPRESSION_SUMMARY),
            TokenCounts(input_tokens=200, output_tokens=50),
            StopEvent(stop_reason="end_turn"),
        ]
        # Turn 2: text
        turn2 = [
            AssistantText(content="Done."),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="end_turn"),
        ]

        class BudgetClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1: seq = turn1
                elif self._call == 2: seq = comp
                else: seq = turn2
                for e in seq: yield e

        client = BudgetClient()
        config = AgentConfig(
            max_turns=3,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1),
        )
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(long_prompt, config, trace_id="t_tok"))

        done = [m for m in msgs if m.type == "done"][0]
        # turn1: 60 + compression: 250 + turn2: 60 = 370
        assert done.total_tokens >= 300  # includes compression costs (250)

    def test_compression_succeeds_but_still_over_threshold(self) -> None:
        """Compression passes but post-compression estimate still > 80% → truncate too."""
        # Turn 1: tool_use
        turn1 = [
            AssistantText(content="ok"),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/t.txt"}),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="tool_use"),
        ]
        # Compression: short summary, doesn't reduce token count much
        comp = [
            AssistantText(content="Valid summary but still large context overall."),
            TokenCounts(input_tokens=100, output_tokens=30),
            StopEvent(stop_reason="end_turn"),
        ]
        # Turn 2: text
        turn2 = [
            AssistantText(content="Done."),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="end_turn"),
        ]

        class BigThenOkClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1: seq = turn1
                elif self._call == 2: seq = comp
                else: seq = turn2
                for e in seq: yield e

        client = BigThenOkClient()
        config = AgentConfig(
            max_turns=3,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1),
        )
        # Use a VERY long prompt so after compression we still exceed threshold
        super_long = "Y" * 100_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(query(super_long, config, trace_id="t_both"))

        compact_msgs = [m for m in msgs if isinstance(m, CompactMessage)]
        assert len(compact_msgs) >= 1
        completed = [m for m in compact_msgs if m.status == "completed"]
        assert len(completed) >= 1
        done = [m for m in msgs if m.type == "done"]
        assert len(done) == 1

    def test_agent_stream_yields_compact_message(self) -> None:
        """Agent.stream() correctly forwards CompactMessage."""
        from agent_core.agent import Agent

        # Turn 1: tool_use
        turn1 = [
            AssistantText(content="ok"),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/t.txt"}),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="tool_use"),
        ]
        # Compression
        comp = [
            AssistantText(content=self.COMPRESSION_SUMMARY),
            TokenCounts(input_tokens=100, output_tokens=30),
            StopEvent(stop_reason="end_turn"),
        ]
        # Turn 2
        turn2 = [
            AssistantText(content="Done."),
            TokenCounts(input_tokens=50, output_tokens=10),
            StopEvent(stop_reason="end_turn"),
        ]

        class AgentClient(MockClient):
            def __init__(self, **kwargs):
                super().__init__([], **kwargs)
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(
                    turn_index=kwargs.get("turn_index", 0),
                    provider="mock", model=self.model,
                )
                self._call += 1
                if self._call == 1: seq = turn1
                elif self._call == 2: seq = comp
                else: seq = turn2
                for e in seq: yield e

        client = AgentClient()
        config = AgentConfig(
            max_turns=3,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1),
        )
        agent = Agent(config)
        long_prompt = "X" * 50_000

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ), patch(
            "agent_core.query.loop.get_context_window",
            return_value=4000,
        ):
            msgs = _collect(agent.stream(long_prompt))

        assert "compact" in [m.type for m in msgs]


# ============================================================================
# CompactMessage model
# ============================================================================


class TestCompactMessageModel:
    def test_serialization_roundtrip(self) -> None:
        cm = CompactMessage(
            status="completed",
            rounds_compressed=3,
            rounds_preserved=2,
            tokens_before=10000,
            tokens_after=3000,
            compression_ratio=0.3,
            summary="A summary.",
            retries_used=1,
        )
        data = cm.model_dump()
        assert data["type"] == "compact"
        assert data["status"] == "completed"
        assert data["compression_ratio"] == 0.3
        assert data["retries_used"] == 1

    def test_in_agent_message_discriminated_union(self) -> None:
        from agent_core.models import AgentMessage

        cm = CompactMessage(status="started")
        # Should be valid as an AgentMessage
        msg: AgentMessage = cm  # type: ignore[assignment]
        assert msg.type == "compact"

    def test_skipped_status(self) -> None:
        cm = CompactMessage(
            status="skipped",
            tokens_before=5000,
            error_detail="All retries exhausted",
        )
        assert cm.status == "skipped"
        assert cm.error_detail == "All retries exhausted"
        assert cm.summary is None
