"""Integration tests for end-to-end session persistence (Item 10, Phase B).

Drives the real ``Agent.stream`` + ``SessionState`` persistence lifecycle
against a scripted ``MockClient``, then verifies the on-disk JSONL and the
``load_session`` recovery path.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_core.agent import Agent
from agent_core.models import AgentConfig
from agent_core.persistence import path as ppath
from agent_core.persistence.session import load_session
from agent_core.persistence.storage import (
    SessionTranscriptWriter,
    load_entries,
)
from agent_core.session import SessionState
from agent_core.llm.events import (
    AssistantText,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from tests.integration.mock_model import MockClient, make_text_turn


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect AGENT_HOME so transcripts land in a temp dir."""
    monkeypatch.setattr(ppath, "AGENT_HOME", str(tmp_path / "agent_home"))
    return str(tmp_path / "workspace")


def _client_factory(client):
    def _factory(_model_id, _config=None):
        return client
    return _factory


async def _drive(agent: Agent, prompt: str) -> None:
    async for _ in agent.stream(prompt):
        pass


def _session_path(ws: str, session_id: str) -> str:
    return ppath.session_jsonl_path(ppath.compute_project_hash(ws), session_id)


# ---------------------------------------------------------------------------
# Single-prompt round-trip
# ---------------------------------------------------------------------------


class TestPersistRoundTrip:
    def test_prompt_persists_and_loads(self, isolated_home, monkeypatch):
        ws = isolated_home
        config = AgentConfig(max_turns=3, workspace_root=ws,
                             permission_mode="bypassPermissions")
        client = MockClient(make_text_turn("hello back"))

        async def _run():
            session = SessionState()
            path = _session_path(ws, session.session_id)
            await session.begin_persistence(path)
            await _drive(Agent(config, session=session), "hi there")
            await session.end_persistence()
            return session.session_id, path

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            sid, path = asyncio.run(_run())

        # File exists with valid TranscriptEntry envelopes
        assert os.path.isfile(path)
        entries = asyncio.run(load_entries(path))
        assert len(entries) >= 2  # user + assistant
        assert all(e.type == "message" and e.sessionId == sid for e in entries)
        roles = [e.message["role"] for e in entries]
        assert "user" in roles and "assistant" in roles

        # load_session reconstructs the same live history
        loaded = asyncio.run(load_session(sid, workspace_root=ws))
        assert loaded is not None
        assert loaded.session_id == sid
        assert loaded._written_count == len(loaded.messages)


# ---------------------------------------------------------------------------
# Cross-prompt continuity — incremental append, not rewrite
# ---------------------------------------------------------------------------


class TestPersistContinuity:
    def test_second_prompt_appends_incrementally(self, isolated_home, monkeypatch):
        ws = isolated_home
        config = AgentConfig(max_turns=3, workspace_root=ws,
                             permission_mode="bypassPermissions")

        class TwoPromptClient(MockClient):
            def __init__(self):
                super().__init__([])
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(turn_index=kwargs.get("turn_index", 0),
                                  provider="mock", model=self.model)
                self._call += 1
                yield AssistantText(content=f"reply {self._call}")
                yield TokenCounts(input_tokens=20, output_tokens=5)
                yield StopEvent(stop_reason="end_turn")

        client = TwoPromptClient()

        async def _run():
            session = SessionState()
            path = _session_path(ws, session.session_id)
            await session.begin_persistence(path)
            await _drive(Agent(config, session=session), "first")
            count_after_1 = len(await load_entries(path))
            await _drive(Agent(config, session=session), "second")
            count_after_2 = len(await load_entries(path))
            await session.end_persistence()
            return session.session_id, path, count_after_1, count_after_2

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            sid, path, c1, c2 = asyncio.run(_run())

        # Second prompt strictly appended (no rewrite/shrink)
        assert c2 > c1
        # All plain message entries, parentUuid chain continuous
        entries = asyncio.run(load_entries(path))
        for prev, cur in zip(entries, entries[1:]):
            assert cur.parentUuid == prev.uuid
        # Loaded history contains both prompts
        loaded = asyncio.run(load_session(sid, workspace_root=ws))
        flat = json.dumps(loaded.messages)
        assert "first" in flat and "second" in flat


# ---------------------------------------------------------------------------
# Resume an interrupted session (dangling tool_use)
# ---------------------------------------------------------------------------


class TestResumeInterrupted:
    def test_dangling_tool_use_cleaned_on_load(self, isolated_home):
        ws = isolated_home
        sid = "interrupted"
        path = _session_path(ws, sid)

        async def _setup_and_load():
            w = SessionTranscriptWriter(path, sid)
            await w.open()
            await w.write_entry(
                {"role": "user", "content": [{"type": "text", "text": "do a thing"}]}
            )
            # assistant called a tool but the result was never written (crash)
            await w.write_entry({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_x", "name": "Read", "input": {}}
                ],
            })
            await w.close()
            return await load_session(sid, workspace_root=ws)

        loaded = asyncio.run(_setup_and_load())
        assert loaded is not None
        # The unresolved tool_use must have been filtered out.
        flat = json.dumps(loaded.messages)
        assert "tool_use" not in flat
        # Only the user prompt survives the cleaning.
        assert any(m["role"] == "user" for m in loaded.messages)


# ---------------------------------------------------------------------------
# Compaction across a persisted boundary → checkpoint, originals preserved
# ---------------------------------------------------------------------------


class TestPersistCompactionBoundary:
    def test_compaction_writes_checkpoint_and_keeps_originals(
        self, isolated_home, monkeypatch
    ):
        """Second prompt triggers in-loop compression that rewrites the
        already-persisted prefix → flush_new_messages appends a checkpoint
        while the original message entries remain on disk (problem #3)."""
        ws = isolated_home

        SUMMARY = "Valid compression summary that is sufficiently long to verify."

        # Phase 1 (prompt 1): a tool_use turn then a text turn, to build rounds.
        turn1a = [
            AssistantText(content="checking"),
            ToolUse(tool_use_id="tu1", tool_name="Read",
                    tool_input={"file_path": "/tmp/x.txt"}),
            TokenCounts(input_tokens=40, output_tokens=10),
            StopEvent(stop_reason="tool_use"),
        ]
        turn1b = [
            AssistantText(content="done one"),
            TokenCounts(input_tokens=40, output_tokens=10),
            StopEvent(stop_reason="end_turn"),
        ]
        # Phase 2 (prompt 2): compression call, then the main text turn.
        comp = [
            AssistantText(content=SUMMARY),
            TokenCounts(input_tokens=80, output_tokens=20),
            StopEvent(stop_reason="end_turn"),
        ]
        turn2 = [
            AssistantText(content="done two"),
            TokenCounts(input_tokens=40, output_tokens=10),
            StopEvent(stop_reason="end_turn"),
        ]

        class PhasedClient(MockClient):
            def __init__(self):
                super().__init__([])
                self._call = 0

            async def stream_messages(self, **kwargs):
                yield StreamStart(turn_index=kwargs.get("turn_index", 0),
                                  provider="mock", model=self.model)
                self._call += 1
                seq = {1: turn1a, 2: turn1b, 3: comp}.get(self._call, turn2)
                for e in seq:
                    yield e

        client = PhasedClient()
        from agent_core.models import CompressionConfig
        config = AgentConfig(
            max_turns=4,
            workspace_root=ws,
            permission_mode="bypassPermissions",
            compression=CompressionConfig(keep_recent_rounds=1, max_retries=1),
        )

        async def _run():
            session = SessionState()
            path = _session_path(ws, session.session_id)
            await session.begin_persistence(path)
            await _drive(Agent(config, session=session), "first prompt")
            await _drive(Agent(config, session=session), "X" * 50_000)
            await session.end_persistence()
            return session.session_id, path

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            m.setattr("agent_core.query.loop.get_context_window", lambda *_: 4000)
            sid, path = asyncio.run(_run())

        entries = asyncio.run(load_entries(path))
        types = [e.type for e in entries]

        # A checkpoint must have been appended for the compaction boundary.
        assert "compaction" in types, f"no checkpoint written: {types}"

        # Original pre-compaction message entries are preserved (FULL audit).
        first_msg = entries[0]
        assert first_msg.type == "message"

        # LIVE recovery yields the compacted snapshot, not the originals.
        loaded = asyncio.run(load_session(sid, workspace_root=ws))
        assert loaded is not None
        assert loaded._compaction_count >= 1
