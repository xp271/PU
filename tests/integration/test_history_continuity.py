"""Integration tests for cross-prompt conversation continuity (SessionState).

Regression coverage for the "layer 2" gap: history must carry from one prompt
to the next within a live session.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from agent_core.agent import Agent
from agent_core.models import AgentConfig
from agent_core.session import SessionState
from tests.integration.mock_model import MockClient, make_text_turn


class _RecordingClient(MockClient):
    """MockClient that records the ``messages`` it receives each call."""

    def __init__(self, script):
        super().__init__(script)
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream_messages(self, messages, tools, system=None, max_tokens=4096, turn_index=0):
        self.seen_messages.append([dict(m) for m in messages])
        async for ev in super().stream_messages(
            messages, tools, system=system, max_tokens=max_tokens, turn_index=turn_index
        ):
            yield ev


def _run(agen) -> list:
    async def _drain():
        out = []
        async for m in agen:
            out.append(m)
        return out

    return asyncio.run(_drain())


def test_done_carries_final_messages():
    client = MockClient(make_text_turn("hi"))
    config = AgentConfig(max_turns=5)
    with patch("agent_core.query.loop.get_client", lambda *_: client):
        session = SessionState()
        agent = Agent(config, session=session)
        msgs = _run(agent.stream("hello"))

    done = next(m for m in msgs if m.type == "done")
    assert done.final_messages is not None
    assert any(m["role"] == "user" for m in done.final_messages)
    # SessionState recorded it
    assert session.messages == done.final_messages


def test_history_continues_across_prompts():
    client = _RecordingClient(make_text_turn("ok"))
    config = AgentConfig(max_turns=5)
    session = SessionState()

    with patch("agent_core.query.loop.get_client", lambda *_: client):
        _run(Agent(config, session=session).stream("remember X=1"))
        _run(Agent(config, session=session).stream("what is X?"))

    # Two prompts → two LLM calls.
    assert len(client.seen_messages) == 2
    first_call, second_call = client.seen_messages

    # First call sees only the first prompt.
    assert len(first_call) == 1
    assert "remember X=1" in str(first_call[0]["content"])

    # Second call sees the *whole* prior conversation plus the new prompt.
    flat = str(second_call)
    assert "remember X=1" in flat
    assert "what is X?" in flat
    assert len(second_call) > len(first_call)


def test_no_session_means_independent_runs():
    client = _RecordingClient(make_text_turn("ok"))
    config = AgentConfig(max_turns=5)

    with patch("agent_core.query.loop.get_client", lambda *_: client):
        _run(Agent(config).stream("first"))
        _run(Agent(config).stream("second"))

    # Without a session, each prompt starts fresh.
    assert all(len(call) == 1 for call in client.seen_messages)
    assert "second" in str(client.seen_messages[1][0]["content"])
    assert "first" not in str(client.seen_messages[1][0]["content"])
