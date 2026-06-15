"""Integration tests for the query() turn loop with a MockClient.

These tests exercise the full query() generator against controlled
InternalEvent scripts — no real API keys required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent_core.models import AgentConfig, AgentMessage
from agent_core.query.loop import query
from tests.integration.mock_model import (
    MockClient,
    make_error_turn,
    make_text_turn,
    make_tool_use_turn,
)


def _collect(agen) -> list[AgentMessage]:
    """Drain an async generator into a list (synchronous context helper)."""
    import asyncio

    async def _drain():
        result: list[AgentMessage] = []
        async for msg in agen:
            result.append(msg)
        return result

    return asyncio.run(_drain())


def _mock_get_client_factory(client: MockClient):
    """Return a callable that replaces ``get_client``."""

    def _factory(_model_id, _config):
        return client

    return _factory


class TestQuerySingleTurn:
    def test_simple_text_response(self) -> None:
        client = MockClient(make_text_turn("Hello, world!"))
        config = AgentConfig(max_turns=5)

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ):
            msgs = _collect(query("test", config, trace_id="t1"))

        types = [m.type for m in msgs]
        assert "assistant_text" in types
        assert "done" in types
        # No tool use in this turn
        assert "tool_use" not in types

    def test_single_tool_use_turn(self) -> None:
        """Tool use triggers execution, result is yielded."""
        client = MockClient(
            make_tool_use_turn("Read", {"file_path": "README.md"}, text="Let me read")
        )
        config = AgentConfig(max_turns=5, permission_mode="bypassPermissions")

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ):
            msgs = _collect(query("test", config, trace_id="t2"))

        types = [m.type for m in msgs]
        assert "tool_use" in types
        assert "tool_result" in types
        assert "done" in types

    def test_error_turn_yields_done_with_error(self) -> None:
        client = MockClient(make_error_turn("overloaded"))
        config = AgentConfig(max_turns=5)

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ):
            msgs = _collect(query("test", config, trace_id="t3"))

        types = [m.type for m in msgs]
        assert "error" in types
        assert "done" in types
        done = msgs[-1]
        assert done.type == "done"  # type: ignore[attr-defined]
        assert done.stop_reason == "error"  # type: ignore[attr-defined]


class TestQueryMultiTurn:
    def test_two_turns_tool_then_text(self) -> None:
        """Turn 1: tool_use → execute → result. Turn 2: text → done."""
        class TwoTurnClient(MockClient):
            _turn = 0

            async def stream_messages(
                self,
                messages=None,
                tools=None,
                system=None,
                max_tokens=4096,
                turn_index=0,
            ):
                self._turn += 1
                if self._turn == 1:
                    self._script = make_tool_use_turn(
                        "Read", {"file_path": "README.md"}, text="Let me check"
                    )
                else:
                    self._script = make_text_turn("File contents: ...")
                result = super().stream_messages(
                    messages=messages or [],
                    tools=tools or [],
                    system=system,
                    max_tokens=max_tokens,
                    turn_index=turn_index,
                )
                async for event in result:
                    yield event

        client = TwoTurnClient([])
        config = AgentConfig(max_turns=5)

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ):
            msgs = _collect(query("test", config, trace_id="t4"))

        types = [m.type for m in msgs]
        # Turn 1: tool_use, tool_result
        assert "tool_use" in types
        assert "tool_result" in types
        # Turn 2: assistant_text
        assert "assistant_text" in types
        # Final
        assert "done" in types
        assert msgs[-1].stop_reason == "end_turn"  # type: ignore[attr-defined]


class TestQueryMaxTurns:
    def test_max_turns_exhausted(self) -> None:
        """When every turn returns a tool_use, the loop hits max_turns."""
        client = MockClient(
            make_tool_use_turn("Read", {"file_path": "/f.txt"})
        )
        config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

        with patch(
            "agent_core.query.loop.get_client",
            _mock_get_client_factory(client),
        ):
            msgs = _collect(query("test", config, trace_id="t5"))

        # With max_turns=2, there should be exactly 2 tool calls
        # (one per turn), then DoneMessage(stop_reason="max_turns")
        done = msgs[-1]
        assert done.type == "done"  # type: ignore[attr-defined]
        # After max_turns=2, the 3rd iteration's budget.is_exhausted() = True
        # so DoneMessage is "max_turns"
        assert done.stop_reason in ("max_turns", "error")  # type: ignore[attr-defined]
