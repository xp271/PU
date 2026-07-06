"""Tests for the SessionState runtime container."""

from __future__ import annotations

import pytest

from agent_core.permissions import SessionPermissionStore
from agent_core.session import SessionState
from agent_core.persistence.storage import load_entries


def test_defaults():
    s = SessionState()
    assert s.messages == []
    assert isinstance(s.permission_store, SessionPermissionStore)
    assert s.session_id
    assert s.transcript_path is None


def test_distinct_sessions_have_distinct_stores():
    a, b = SessionState(), SessionState()
    assert a.permission_store is not b.permission_store
    assert a.session_id != b.session_id


def test_snapshot_returns_copy():
    s = SessionState(messages=[{"role": "user", "content": "hi"}])
    snap = s.snapshot_for_next_turn()
    snap.append({"role": "user", "content": "extra"})
    assert len(s.messages) == 1


def test_record_turn_result_replaces_history():
    s = SessionState(messages=[{"role": "user", "content": "old"}])
    final = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    s.record_turn_result(final)
    assert s.messages == final
    # stored as a copy, not the same list object
    assert s.messages is not final


def test_record_turn_result_none_keeps_history():
    s = SessionState(messages=[{"role": "user", "content": "old"}])
    s.record_turn_result(None)
    assert s.messages == [{"role": "user", "content": "old"}]


def test_clear_history_keeps_permission_memory():
    s = SessionState()
    s.permission_store.add_allow("Bash")
    s.messages.append({"role": "user", "content": "hi"})
    s.clear_history()
    assert s.messages == []
    assert s.permission_store.rules()  # permissions survive /clear


def test_workspace_override_default():
    """workspace_override defaults to None."""
    s = SessionState()
    assert s.workspace_override is None


def test_clear_history_resets_written_count():
    """clear_history() also resets _written_count to 0."""
    s = SessionState()
    s._written_count = 5
    s.messages.append({"role": "user", "content": "hi"})
    s.clear_history()
    assert s.messages == []
    assert s._written_count == 0



# ---------------------------------------------------------------------------
# Phase B: persistence lifecycle (Item 10)
# ---------------------------------------------------------------------------

def _msg(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


class TestBeginEndPersistence:
    @pytest.mark.asyncio
    async def test_fresh_session_writes_nothing_until_flush(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        assert s.transcript_path == p
        assert s._writer is not None and s._writer.is_open
        await s.end_persistence()

        # Empty session → empty (or absent) file, zero entries
        entries = await load_entries(p)
        assert entries == []

    @pytest.mark.asyncio
    async def test_begin_persistence_writes_preexisting_messages(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState(messages=[_msg("user", "hi"), _msg("assistant", "ok")])
        await s.begin_persistence(p)
        await s.end_persistence()

        entries = await load_entries(p)
        assert len(entries) == 2
        assert s._written_count == 2

    @pytest.mark.asyncio
    async def test_end_persistence_idempotent(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        await s.end_persistence()
        await s.end_persistence()  # must not raise
        assert s._writer is None

    @pytest.mark.asyncio
    async def test_resume_does_not_reappend_history(self, tmp_path):
        """Problem #1: a resumed session (pre-set _written_count) must not
        duplicate already-persisted messages when persistence re-opens."""
        p = str(tmp_path / "s.jsonl")
        s = SessionState(messages=[_msg("user", "a"), _msg("assistant", "b")])
        await s.begin_persistence(p)
        await s.end_persistence()
        assert len(await load_entries(p)) == 2

        # Simulate resume: same messages already on disk, watermark pre-set.
        s2 = SessionState(
            session_id=s.session_id,
            messages=[_msg("user", "a"), _msg("assistant", "b")],
        )
        s2._written_count = 2
        await s2.begin_persistence(p)
        await s2.end_persistence()

        # No duplication — still 2 entries.
        assert len(await load_entries(p)) == 2


class TestFlushNewMessages:
    @pytest.mark.asyncio
    async def test_incremental_append(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)

        await s.flush_new_messages([_msg("user", "q1")])
        await s.flush_new_messages([_msg("user", "q1"), _msg("assistant", "a1")])
        await s.end_persistence()

        entries = await load_entries(p)
        assert [e.message["content"][0]["text"] for e in entries] == ["q1", "a1"]
        assert all(e.type == "message" for e in entries)

    @pytest.mark.asyncio
    async def test_none_is_noop(self, tmp_path):
        """B1 fallback: None final_messages must not crash."""
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        await s.flush_new_messages(None)  # must not raise
        await s.end_persistence()
        assert await load_entries(p) == []

    @pytest.mark.asyncio
    async def test_noop_when_writer_absent(self):
        s = SessionState()
        # No begin_persistence → writer is None
        await s.flush_new_messages([_msg("user", "x")])  # must not raise

    @pytest.mark.asyncio
    async def test_parent_uuid_chain_continuous(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        await s.flush_new_messages([_msg("user", "q1")])
        await s.flush_new_messages([_msg("user", "q1"), _msg("assistant", "a1")])
        await s.end_persistence()

        entries = await load_entries(p)
        assert entries[0].parentUuid is None
        assert entries[1].parentUuid == entries[0].uuid

    @pytest.mark.asyncio
    async def test_resume_keeps_chain_continuous(self, tmp_path):
        """B2: after reopen, the next entry's parentUuid links to the last
        on-disk entry rather than starting a new root."""
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        await s.flush_new_messages([_msg("user", "q1")])
        await s.end_persistence()

        first = (await load_entries(p))[0]

        s2 = SessionState(session_id=s.session_id, messages=[_msg("user", "q1")])
        s2._written_count = 1
        await s2.begin_persistence(p)
        await s2.flush_new_messages([_msg("user", "q1"), _msg("assistant", "a1")])
        await s2.end_persistence()

        entries = await load_entries(p)
        assert len(entries) == 2
        assert entries[1].parentUuid == first.uuid  # chain survives resume


class TestCompactionDetection:
    @pytest.mark.asyncio
    async def test_compaction_writes_checkpoint_and_preserves_originals(self, tmp_path):
        """Problem #3: compaction appends a checkpoint; originals stay on disk."""
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)

        # Grow to 4 messages
        live = [_msg("user", "m1"), _msg("assistant", "m2"),
                _msg("user", "m3"), _msg("assistant", "m4")]
        await s.flush_new_messages(live)

        # Compaction: head replaced by a summary, keep last message verbatim
        compacted = [_msg("system", "SUMMARY"), _msg("assistant", "m4")]
        await s.flush_new_messages(compacted)
        await s.end_persistence()

        entries = await load_entries(p)
        # 4 original message entries + 1 checkpoint
        assert len(entries) == 5
        assert [e.type for e in entries] == [
            "message", "message", "message", "message", "compaction",
        ]
        # Originals preserved (FULL audit)
        assert entries[0].message["content"][0]["text"] == "m1"
        # Checkpoint snapshot == compacted live list
        cp = entries[4].compaction
        assert cp is not None
        assert cp.segment_index == 1
        assert cp.kept_count == 1            # m4 retained
        assert cp.summary_count == 1         # SUMMARY
        assert len(cp.snapshot) == 2
        # Watermark aligns with new live length
        assert s._written_count == 2

    @pytest.mark.asyncio
    async def test_segment_index_increments_with_resume(self, tmp_path):
        """B3: segment_index keeps increasing across resume."""
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        s._compaction_count = 2  # as if two checkpoints already happened
        await s.begin_persistence(p)
        await s.flush_new_messages([_msg("user", "m1"), _msg("assistant", "m2")])
        await s.flush_new_messages([_msg("system", "SUM")])
        await s.end_persistence()

        entries = await load_entries(p)
        cp = next(e for e in entries if e.type == "compaction")
        assert cp.compaction.segment_index == 3  # 2 + 1

    @pytest.mark.asyncio
    async def test_no_false_compaction_on_pure_growth(self, tmp_path):
        p = str(tmp_path / "s.jsonl")
        s = SessionState()
        await s.begin_persistence(p)
        await s.flush_new_messages([_msg("user", "m1")])
        await s.flush_new_messages([_msg("user", "m1"), _msg("assistant", "m2")])
        await s.end_persistence()

        entries = await load_entries(p)
        assert all(e.type == "message" for e in entries)  # no checkpoint


# ---------------------------------------------------------------------------
# Checkpoint (v1): checkpoint_dict / restore_checkpoint
# ---------------------------------------------------------------------------


def _user_msg(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


class TestCheckpointDict:
    def test_checkpoint_dict_contains_all_fields(self):
        """checkpoint_dict() includes all expected top-level keys."""
        s = SessionState()
        d = s.checkpoint_dict()
        for key in (
            "version", "session_id", "title", "created_at", "last_active_at",
            "message_count", "written_count", "compaction_count",
            "workspace_override", "permission_rules",
        ):
            assert key in d, f"missing key: {key}"
        assert d["version"] == 1
        assert d["session_id"] == s.session_id
        assert d["workspace_override"] is None
        assert d["permission_rules"] == []

    def test_checkpoint_dict_extracts_title_from_first_user_msg(self):
        """checkpoint_dict() extracts title from first user message, truncated."""
        s = SessionState(messages=[
            _user_msg("A" * 120),
            _user_msg("second"),
        ])
        d = s.checkpoint_dict()
        assert len(d["title"]) == 100
        assert d["title"].startswith("A" * 100)

    def test_checkpoint_dict_title_empty_message(self):
        """checkpoint_dict() handles user message with empty text."""
        s = SessionState(messages=[
            {"role": "user", "content": [{"type": "text", "text": ""}]},
        ])
        d = s.checkpoint_dict()
        assert d["title"] == "(空消息)"

    def test_checkpoint_dict_includes_permission_rules(self):
        """checkpoint_dict() serializes session permission rules."""
        s = SessionState()
        s.permission_store.add_allow("Bash")
        s.permission_store.add_deny("Write")
        d = s.checkpoint_dict()
        assert len(d["permission_rules"]) == 2
        actions = {r["action"] for r in d["permission_rules"]}
        assert actions == {"allow", "deny"}


class TestRestoreCheckpoint:
    def test_restore_checkpoint_applies_workspace_override(self):
        """restore_checkpoint() restores workspace_override."""
        s = SessionState()
        assert s.workspace_override is None
        s.restore_checkpoint({"workspace_override": "/tmp/test"})
        assert s.workspace_override == "/tmp/test"

    def test_restore_checkpoint_applies_permission_rules(self):
        """restore_checkpoint() restores permission_rules."""
        s = SessionState()
        assert len(s.permission_store.rules()) == 0

        s.restore_checkpoint({
            "permission_rules": [
                {
                    "tool_pattern": "Bash",
                    "action": "allow",
                    "priority": 20,
                    "source": "session",
                    "description": "Session allow for 'Bash'.",
                }
            ],
        })
        assert len(s.permission_store.rules()) == 1
        assert s.permission_store.rules()[0].action == "allow"

    def test_restore_checkpoint_idempotent(self):
        """Repeated restore_checkpoint() does not duplicate rules."""
        s = SessionState()
        rules = [
            {
                "tool_pattern": "Bash",
                "action": "allow",
                "priority": 20,
                "source": "session",
                "description": "Session allow for 'Bash'.",
            },
        ]
        s.restore_checkpoint({"permission_rules": rules})
        s.restore_checkpoint({"permission_rules": rules})
        assert len(s.permission_store.rules()) == 1

    def test_restore_checkpoint_does_not_overwrite_counts(self):
        """restore_checkpoint() does NOT overwrite _written_count / _compaction_count."""
        s = SessionState()
        s._written_count = 99
        s._compaction_count = 5
        s.restore_checkpoint({"written_count": 10, "compaction_count": 1})
        assert s._written_count == 99
        assert s._compaction_count == 5

    def test_restore_checkpoint_preserves_timestamps(self):
        """restore_checkpoint() applies created_at / last_active_at."""
        s = SessionState()
        s.restore_checkpoint({
            "created_at": "2026-01-01T00:00:00",
            "last_active_at": "2026-07-01T12:00:00",
        })
        assert s._created_at == "2026-01-01T00:00:00"
        assert s._last_active_at == "2026-07-01T12:00:00"
