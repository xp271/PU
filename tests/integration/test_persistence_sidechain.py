"""Integration tests for Phase B sub-agent sidechain persistence (Item 10).

When ``run_agent`` is given ``session_id`` + ``project_hash``, sidechain
records switch from the legacy bare ``{role, content}`` format to the
``TranscriptEntry`` envelope, and ``_load_sidechain_messages`` must unwrap
``.message`` on resume (problem #2).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.run import run_agent
from agent_core.models import AgentConfig
from agent_core.persistence import path as ppath
from tests.integration.mock_model import MockClient, make_tool_use_turn


def _collect(agen):
    async def _drain():
        out = []
        async for msg in agen:
            out.append(msg)
        return out
    return asyncio.run(_drain())


def _factory(client):
    def f(_model_id, _config):
        return client
    return f


@pytest.fixture
def sidechain_dir(tmp_path, monkeypatch):
    d = tmp_path / "side"
    d.mkdir()
    monkeypatch.setattr(
        "agent_core.agents.run._sidechain_dir",
        lambda _trace, session_id=None, project_hash=None: str(d),
    )
    return d


# ---------------------------------------------------------------------------
# Phase B envelope format
# ---------------------------------------------------------------------------


class TestSidechainEnvelopeFormat:
    def test_records_use_transcript_entry_envelope(self, sidechain_dir):
        client = MockClient(make_tool_use_turn("Bash", {"command": "ls"}))
        definition = AgentDefinition(agent_type="t", tools=["Read"])
        config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _factory(client)):
            _collect(
                run_agent(
                    "do x",
                    definition,
                    parent_config=config,
                    parent_trace_id="trace-1",
                    parent_allowed_tools=frozenset({"Read"}),
                    session_id="sess-1",
                    project_hash="hash-1",
                )
            )

        files = list(Path(sidechain_dir).glob("*.jsonl"))
        assert files, "a sidechain file should be created"
        records = [
            json.loads(ln)
            for ln in files[0].read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert records
        for rec in records:
            # Phase B envelope fields
            assert "uuid" in rec
            assert rec["sessionId"] == "sess-1"
            assert rec["isSidechain"] is True
            assert rec.get("type", "message") == "message"
            # The actual message is wrapped, not at top level
            assert "message" in rec
            assert rec["message"]["role"] in ("user", "assistant")
            assert "role" not in rec  # not a bare record

    def test_parent_uuid_chain_within_sidechain(self, sidechain_dir):
        # Two turns → multiple records → linear parentUuid chain.
        client = MockClient(make_tool_use_turn("Bash", {"command": "ls"}))
        definition = AgentDefinition(agent_type="t", tools=["Read"])
        config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _factory(client)):
            _collect(
                run_agent(
                    "do x", definition,
                    parent_config=config, parent_trace_id="trace-2",
                    parent_allowed_tools=frozenset({"Read"}),
                    session_id="sess-2", project_hash="hash-2",
                )
            )

        files = list(Path(sidechain_dir).glob("*.jsonl"))
        records = [
            json.loads(ln)
            for ln in files[0].read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        if len(records) >= 2:
            for prev, cur in zip(records, records[1:]):
                assert cur["parentUuid"] == prev["uuid"]


# ---------------------------------------------------------------------------
# Unwrap on resume (problem #2)
# ---------------------------------------------------------------------------


class TestSidechainUnwrap:
    def test_load_sidechain_unwraps_envelope(self, sidechain_dir):
        """_load_sidechain_messages must return bare {role, content} dicts,
        not TranscriptEntry envelopes (otherwise resume feeds garbage)."""
        from agent_core.agents.fork import _load_sidechain_messages
        from agent_core.persistence.storage import (
            SessionTranscriptWriter,
        )

        path = os.path.join(str(sidechain_dir), "agent-x.jsonl")

        async def _setup_and_load():
            w = SessionTranscriptWriter(path, "sess-3")
            await w.open()
            await w.write_entry(
                {"role": "user", "content": [{"type": "text", "text": "task"}]},
                is_sidechain=True,
            )
            await w.write_entry(
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                is_sidechain=True,
            )
            await w.close()
            return await _load_sidechain_messages(path)

        messages = asyncio.run(_setup_and_load())
        assert len(messages) == 2
        for m in messages:
            # Unwrapped: top-level role/content, no envelope keys
            assert m["role"] in ("user", "assistant")
            assert "content" in m
            assert "uuid" not in m and "message" not in m

    def test_load_sidechain_legacy_bare_format_still_works(self, sidechain_dir):
        """Backward compatibility: old bare-record files load unchanged."""
        from agent_core.agents.fork import _load_sidechain_messages

        path = os.path.join(str(sidechain_dir), "legacy.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n")
            fh.write(json.dumps({"role": "assistant", "content": "ok"}) + "\n")

        messages = asyncio.run(_load_sidechain_messages(path))
        assert len(messages) == 2
        assert messages[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Path scheme
# ---------------------------------------------------------------------------


class TestSidechainPathScheme:
    def test_phase_b_path_layout(self):
        p = ppath.sidechain_jsonl_path("hashABC", "sessXYZ", "agent-7")
        assert p.endswith(
            os.path.join("projects", "hashABC", "sessXYZ", "agent-7.jsonl")
        )
