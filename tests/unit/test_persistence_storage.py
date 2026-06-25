"""Unit tests for persistence/storage.py."""

import json, os
from unittest.mock import patch

import pytest

from agent_core.persistence.storage import (
    CompactionRecord,
    SessionTranscriptWriter,
    TranscriptEntry,
    load_entries,
    save_entries,
)


# ---------------------------------------------------------------------------
# TranscriptEntry
# ---------------------------------------------------------------------------

class TestTranscriptEntry:
    def test_from_message(self):
        msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        entry = TranscriptEntry.from_message(msg, session_id="sid1")
        assert entry.type == "message"
        assert entry.message == msg
        assert entry.sessionId == "sid1"
        assert entry.isSidechain is False
        assert entry.version == 1
        assert len(entry.uuid) == 32  # hex uuid4
        assert entry.parentUuid is None
        assert entry.compaction is None

    def test_from_message_with_parent(self):
        msg = {"role": "assistant", "content": "ok"}
        entry = TranscriptEntry.from_message(
            msg, session_id="sid1", parent_uuid="prev-uuid"
        )
        assert entry.parentUuid == "prev-uuid"

    def test_from_message_sidechain(self):
        entry = TranscriptEntry.from_message(
            {"role": "user", "content": "x"},
            session_id="sid1",
            is_sidechain=True,
        )
        assert entry.isSidechain is True

    def test_from_checkpoint(self):
        record = CompactionRecord(
            segment_index=1,
            snapshot=[{"role": "user", "content": "summary"}],
            summary_count=1,
            kept_count=0,
        )
        entry = TranscriptEntry.from_checkpoint(record, session_id="sid1")
        assert entry.type == "compaction"
        assert entry.message is None
        assert entry.compaction is not None
        assert entry.compaction.segment_index == 1

    def test_roundtrip_json(self):
        entry = TranscriptEntry.from_message(
            {"role": "user", "content": "hello"}, session_id="sid1"
        )
        raw = entry.model_dump_json()
        parsed = TranscriptEntry(**json.loads(raw))
        assert parsed.uuid == entry.uuid
        assert parsed.message == entry.message


# ---------------------------------------------------------------------------
# CompactionRecord
# ---------------------------------------------------------------------------

class TestCompactionRecord:
    def test_minimal(self):
        r = CompactionRecord(
            segment_index=1,
            snapshot=[{"role": "user", "content": "s"}],
            summary_count=1,
            kept_count=0,
        )
        assert r.segment_index == 1
        assert r.summarized_count == 0
        assert r.trigger == "token_threshold"

    def test_with_audit_fields(self):
        r = CompactionRecord(
            segment_index=2,
            snapshot=[],
            summary_count=0,
            kept_count=0,
            summarized_uuid_first="u1",
            summarized_uuid_last="u5",
            summarized_count=5,
            prev_checkpoint_uuid="cp1",
        )
        assert r.summarized_count == 5
        assert r.prev_checkpoint_uuid == "cp1"


# ---------------------------------------------------------------------------
# SessionTranscriptWriter
# ---------------------------------------------------------------------------

class TestSessionTranscriptWriter:
    @pytest.fixture
    def writer_path(self, tmp_path):
        return str(tmp_path / "test.jsonl")

    @pytest.mark.asyncio
    async def test_open_close(self, writer_path):
        w = SessionTranscriptWriter(writer_path, "sid1")
        assert not w.is_open
        await w.open()
        assert w.is_open
        await w.close()
        assert not w.is_open

    @pytest.mark.asyncio
    async def test_close_idempotent(self, writer_path):
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        await w.close()
        await w.close()  # should not raise

    @pytest.mark.asyncio
    async def test_write_entry(self, writer_path):
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        uid = await w.write_entry({"role": "user", "content": "hi"})
        assert uid is not None
        assert w.entry_count == 1
        assert w.last_uuid == uid
        await w.close()

        # Verify file contents
        entries = await load_entries(writer_path)
        assert len(entries) == 1
        assert entries[0].uuid == uid
        assert entries[0].message == {"role": "user", "content": "hi"}
        assert entries[0].type == "message"

    @pytest.mark.asyncio
    async def test_parent_uuid_chain(self, writer_path):
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        uid1 = await w.write_entry({"role": "user", "content": "m1"})
        uid2 = await w.write_entry({"role": "assistant", "content": "m2"})
        await w.close()

        entries = await load_entries(writer_path)
        assert entries[0].parentUuid is None
        assert entries[1].parentUuid == uid1

    @pytest.mark.asyncio
    async def test_write_checkpoint(self, writer_path):
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        await w.write_entry({"role": "user", "content": "pre"})
        record = CompactionRecord(
            segment_index=1,
            snapshot=[{"role": "user", "content": "summary"}],
            summary_count=1,
            kept_count=0,
        )
        cid = await w.write_checkpoint(record)
        assert cid is not None
        assert w.last_checkpoint_uuid == cid
        await w.write_entry({"role": "user", "content": "post"})
        await w.close()

        entries = await load_entries(writer_path)
        assert len(entries) == 3
        assert entries[0].type == "message"
        assert entries[1].type == "compaction"
        assert entries[1].compaction.segment_index == 1
        assert entries[2].type == "message"

    @pytest.mark.asyncio
    async def test_checkpoint_preserves_old_entries(self, writer_path):
        """Problem #3: checkpoint appends, never deletes."""
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        for i in range(5):
            await w.write_entry({"role": "user", "content": f"m{i}"})
        record = CompactionRecord(
            segment_index=1,
            snapshot=[{"role": "user", "content": "summary"}],
            summary_count=1,
            kept_count=0,
        )
        await w.write_checkpoint(record)
        await w.close()

        entries = await load_entries(writer_path)
        assert len(entries) == 6  # 5 msgs + 1 checkpoint
        # Original entries intact
        for i in range(5):
            assert entries[i].message == {"role": "user", "content": f"m{i}"}

    # -- Problem #4: crash guard ----------------------------------------------

    @pytest.mark.asyncio
    async def test_open_fixes_trailing_half_line(self, writer_path):
        """Problem #4: crash left a half-written line with no newline."""
        # Simulate a crash: write partial line
        with open(writer_path, "w", encoding="utf-8") as f:
            f.write('{"uuid":"bad",')  # no trailing newline

        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        # The half-line should have been isolated with a newline
        # Write a valid entry
        await w.write_entry({"role": "user", "content": "valid"})
        await w.close()

        entries = await load_entries(writer_path)
        # The corrupted half-line is skipped; valid entry survives
        assert any(e.message == {"role": "user", "content": "valid"} for e in entries)

    @pytest.mark.asyncio
    async def test_open_empty_file(self, writer_path):
        """Open on a nonexistent or empty file should work fine."""
        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        await w.write_entry({"role": "user", "content": "ok"})
        await w.close()
        entries = await load_entries(writer_path)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_reopen_seeds_chain_state(self, writer_path):
        """B2: reopening an existing transcript restores _last_uuid so the
        parentUuid chain stays continuous across resume."""
        w1 = SessionTranscriptWriter(writer_path, "sid1")
        await w1.open()
        u1 = await w1.write_entry({"role": "user", "content": "m1"})
        await w1.close()

        w2 = SessionTranscriptWriter(writer_path, "sid1")
        await w2.open()
        assert w2.last_uuid == u1  # recovered
        u2 = await w2.write_entry({"role": "assistant", "content": "m2"})
        await w2.close()

        entries = await load_entries(writer_path)
        assert len(entries) == 2
        assert entries[1].parentUuid == u1  # chain not broken by reopen

    @pytest.mark.asyncio
    async def test_reopen_recovers_last_checkpoint(self, writer_path):
        w1 = SessionTranscriptWriter(writer_path, "sid1")
        await w1.open()
        await w1.write_entry({"role": "user", "content": "m1"})
        cid = await w1.write_checkpoint(
            CompactionRecord(segment_index=1, snapshot=[], summary_count=0, kept_count=0)
        )
        await w1.close()

        w2 = SessionTranscriptWriter(writer_path, "sid1")
        await w2.open()
        assert w2.last_checkpoint_uuid == cid

    @pytest.mark.asyncio
    async def test_crash_guard_isolates_multiple_runs(self, writer_path):
        """A half-line from a crash is isolated; the next run appends cleanly
        and earlier valid lines survive."""
        # Valid line + crashed half-line (no newline).
        with open(writer_path, "w", encoding="utf-8") as f:
            good = TranscriptEntry.from_message({"role": "user", "content": "good"}, "sid1")
            f.write(good.model_dump_json() + "\n")
            f.write('{"uuid":"partial"')  # crash mid-write

        w = SessionTranscriptWriter(writer_path, "sid1")
        await w.open()
        await w.write_entry({"role": "assistant", "content": "after"})
        await w.close()

        entries = await load_entries(writer_path)
        contents = [e.message["content"] for e in entries if e.message]
        assert "good" in contents
        assert "after" in contents
        # The partial line is not resurrected as a valid entry.
        assert all(e.uuid != "partial" for e in entries)


# ---------------------------------------------------------------------------
# load_entries / save_entries
# ---------------------------------------------------------------------------


class TestLoadSaveEntries:
    @pytest.mark.asyncio
    async def test_load_empty_file(self, tmp_path):
        p = str(tmp_path / "nonexistent.jsonl")
        entries = await load_entries(p)
        assert entries == []

    @pytest.mark.asyncio
    async def test_save_and_reload(self, tmp_path):
        p = str(tmp_path / "test.jsonl")
        e1 = TranscriptEntry.from_message(
            {"role": "user", "content": "hi"}, session_id="sid1"
        )
        e2 = TranscriptEntry.from_message(
            {"role": "assistant", "content": "hi back"}, session_id="sid1"
        )
        await save_entries(p, [e1, e2])
        loaded = await load_entries(p)
        assert len(loaded) == 2
        assert loaded[0].uuid == e1.uuid
        assert loaded[1].uuid == e2.uuid

    @pytest.mark.asyncio
    async def test_skip_bad_lines(self, tmp_path):
        p = str(tmp_path / "bad.jsonl")
        # Write one valid, one bad, one valid
        valid1 = TranscriptEntry.from_message({"role": "user", "content": "v1"}, session_id="sid1")
        with open(p, "w", encoding="utf-8") as f:
            f.write(valid1.model_dump_json() + "\n")
            f.write("this is not json\n")
            f.write(valid1.model_dump_json() + "\n")
        loaded = await load_entries(p)
        # Bad line skipped; 2 valid entries remain
        assert len(loaded) == 2
