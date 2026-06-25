"""Unit tests for persistence/session.py — list / load / delete / cleanup."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.persistence import path as ppath
from agent_core.persistence.session import (
    SessionInfo,
    cleanup_old_sessions,
    delete_session,
    list_sessions,
    load_session,
)
from agent_core.persistence.storage import (
    CompactionRecord,
    SessionTranscriptWriter,
    TranscriptEntry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point AGENT_HOME at a temp dir so sessions land in an isolated place."""
    home = tmp_path / "agent_home"
    monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
    # The workspace root used to derive project_hash.
    ws = str(tmp_path / "proj")
    os.makedirs(ws, exist_ok=True)
    return ws


def _ts(dt: datetime) -> str:
    return dt.isoformat()


async def _write_session(workspace_root, session_id, messages, *, timestamps=None):
    """Write a session JSONL via the real writer; optionally override timestamps."""
    project_hash = ppath.compute_project_hash(workspace_root)
    p = ppath.session_jsonl_path(project_hash, session_id)
    w = SessionTranscriptWriter(p, session_id)
    await w.open()
    for m in messages:
        await w.write_entry(m)
    await w.close()

    if timestamps is not None:
        # Rewrite with explicit timestamps for deterministic ordering tests.
        lines = []
        with open(p, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                obj = json.loads(line)
                obj["timestamp"] = timestamps[i]
                lines.append(json.dumps(obj))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return p


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    @pytest.mark.asyncio
    async def test_empty_dir(self, workspace):
        assert await list_sessions(workspace) == []

    @pytest.mark.asyncio
    async def test_single_session_metadata(self, workspace):
        await _write_session(
            workspace, "sess-1",
            [_user("first prompt here"), _assistant("a reply")],
        )
        sessions = await list_sessions(workspace)
        assert len(sessions) == 1
        s = sessions[0]
        assert isinstance(s, SessionInfo)
        assert s.session_id == "sess-1"
        assert s.title == "first prompt here"
        assert s.message_count == 2
        assert s.has_unfinished_turn is False

    @pytest.mark.asyncio
    async def test_sorted_by_last_activity(self, workspace):
        now = datetime.now(timezone.utc)
        await _write_session(
            workspace, "old", [_user("old"), _assistant("x")],
            timestamps=[_ts(now - timedelta(hours=2)), _ts(now - timedelta(hours=2))],
        )
        await _write_session(
            workspace, "new", [_user("new"), _assistant("y")],
            timestamps=[_ts(now - timedelta(minutes=1)), _ts(now)],
        )
        sessions = await list_sessions(workspace)
        assert [s.session_id for s in sessions] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_unfinished_turn_flag(self, workspace):
        # Last message is a user prompt (no assistant response) → unfinished.
        await _write_session(
            workspace, "sess-x",
            [_user("do it"), _assistant("ok"), _user("and again")],
        )
        sessions = await list_sessions(workspace)
        assert sessions[0].has_unfinished_turn is True

    @pytest.mark.asyncio
    async def test_corrupt_first_line_skipped(self, workspace):
        project_hash = ppath.compute_project_hash(workspace)
        os.makedirs(ppath.project_dir(project_hash), exist_ok=True)
        p = ppath.session_jsonl_path(project_hash, "broken")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        # Corrupt-first-line session is dropped from the listing, not crash.
        sessions = await list_sessions(workspace)
        assert all(s.session_id != "broken" for s in sessions)


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------


class TestLoadSession:
    @pytest.mark.asyncio
    async def test_roundtrip(self, workspace):
        await _write_session(
            workspace, "rt",
            [_user("hello"), _assistant("world")],
        )
        session = await load_session("rt", workspace_root=workspace)
        assert session is not None
        assert session.session_id == "rt"
        assert len(session.messages) == 2
        # Watermark pre-set so begin_persistence won't re-append (problem #1).
        assert session._written_count == 2

    @pytest.mark.asyncio
    async def test_nonexistent_returns_none(self, workspace):
        assert await load_session("ghost", workspace_root=workspace) is None

    @pytest.mark.asyncio
    async def test_empty_file_returns_none(self, workspace):
        project_hash = ppath.compute_project_hash(workspace)
        os.makedirs(ppath.project_dir(project_hash), exist_ok=True)
        p = ppath.session_jsonl_path(project_hash, "empty")
        open(p, "w").close()
        assert await load_session("empty", workspace_root=workspace) is None

    @pytest.mark.asyncio
    async def test_all_corrupt_returns_none(self, workspace):
        project_hash = ppath.compute_project_hash(workspace)
        os.makedirs(ppath.project_dir(project_hash), exist_ok=True)
        p = ppath.session_jsonl_path(project_hash, "garbage")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("xxx\nyyy\n")
        assert await load_session("garbage", workspace_root=workspace) is None

    @pytest.mark.asyncio
    async def test_resume_after_compaction_returns_live(self, workspace):
        """A session with a checkpoint resolves to the LIVE list and pre-sets
        both _written_count and _compaction_count."""
        project_hash = ppath.compute_project_hash(workspace)
        p = ppath.session_jsonl_path(project_hash, "comp")
        w = SessionTranscriptWriter(p, "comp")
        await w.open()
        await w.write_entry(_user("old1"))
        await w.write_entry(_assistant("old2"))
        await w.write_checkpoint(
            CompactionRecord(
                segment_index=1,
                snapshot=[_user("SUMMARY"), _assistant("old2")],
                summary_count=1,
                kept_count=1,
            )
        )
        await w.write_entry(_user("new"))
        await w.close()

        session = await load_session("comp", workspace_root=workspace)
        assert session is not None
        # LIVE = snapshot (2) + post-checkpoint message (1) = 3
        assert len(session.messages) == 3
        assert session._written_count == 3
        assert session._compaction_count == 1

    @pytest.mark.asyncio
    async def test_migrates_v0_on_load(self, workspace):
        """Problem #6: bare v0 lines are migrated before recovery."""
        project_hash = ppath.compute_project_hash(workspace)
        os.makedirs(ppath.project_dir(project_hash), exist_ok=True)
        p = ppath.session_jsonl_path(project_hash, "v0")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_user("legacy")) + "\n")
            fh.write(json.dumps(_assistant("reply")) + "\n")

        session = await load_session("v0", workspace_root=workspace)
        assert session is not None
        assert len(session.messages) == 2


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_delete_existing(self, workspace):
        await _write_session(workspace, "del", [_user("x")])
        ok = await delete_session("del", workspace_root=workspace)
        assert ok is True
        assert await load_session("del", workspace_root=workspace) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, workspace):
        assert await delete_session("ghost", workspace_root=workspace) is False

    @pytest.mark.asyncio
    async def test_delete_removes_sidechain_dir(self, workspace):
        await _write_session(workspace, "withside", [_user("x")])
        project_hash = ppath.compute_project_hash(workspace)
        side = ppath.sidechain_dir(project_hash, "withside")
        os.makedirs(side, exist_ok=True)
        with open(os.path.join(side, "agent-1.jsonl"), "w") as fh:
            fh.write("{}\n")

        await delete_session("withside", workspace_root=workspace)
        assert not os.path.isdir(side)


# ---------------------------------------------------------------------------
# cleanup_old_sessions
# ---------------------------------------------------------------------------


class TestCleanupOldSessions:
    @pytest.mark.asyncio
    async def test_removes_only_old(self, workspace):
        now = datetime.now(timezone.utc)
        await _write_session(
            workspace, "ancient", [_user("a"), _assistant("b")],
            timestamps=[_ts(now - timedelta(days=40))] * 2,
        )
        await _write_session(
            workspace, "fresh", [_user("a"), _assistant("b")],
            timestamps=[_ts(now)] * 2,
        )
        deleted = await cleanup_old_sessions(workspace, max_age_days=30)
        assert deleted == ["ancient"]
        assert await load_session("fresh", workspace_root=workspace) is not None

    @pytest.mark.asyncio
    async def test_dry_run_keeps_files(self, workspace):
        now = datetime.now(timezone.utc)
        await _write_session(
            workspace, "ancient", [_user("a")],
            timestamps=[_ts(now - timedelta(days=99))],
        )
        deleted = await cleanup_old_sessions(workspace, max_age_days=30, dry_run=True)
        assert deleted == ["ancient"]
        # File still present because dry_run.
        assert await load_session("ancient", workspace_root=workspace) is not None
