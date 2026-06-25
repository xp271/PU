"""Unit tests for persistence/migrate.py."""

import json
import os

import pytest

from agent_core.persistence.migrate import (
    migrate_transcript,
    needs_migration,
)
from agent_core.persistence.storage import load_entries, TranscriptEntry


class TestNeedsMigration:
    def test_v1_no_migration_needed(self):
        entries = [
            {"uuid": "abc", "version": 1, "type": "message", "message": {"role": "user"}},
        ]
        assert not needs_migration(entries)

    def test_v0_needs_migration(self):
        entries = [
            {"role": "user", "content": "hello"},
        ]
        assert needs_migration(entries)

    def test_empty_no_migration(self):
        assert not needs_migration([])


class TestMigrateTranscript:
    @pytest.mark.asyncio
    async def test_v0_to_v1_upgrade(self, tmp_path):
        # Write old v0 format (bare message dicts)
        p = str(tmp_path / "v0.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]}) + "\n")

        migrated = await migrate_transcript(p)
        assert migrated is True

        # Read back — should now be v1 TranscriptEntry envelopes
        entries = await load_entries(p)
        assert len(entries) == 2
        for e in entries:
            assert e.version == 1
            assert e.type == "message"
            assert e.message is not None
            assert e.message["role"] in ("user", "assistant")

    @pytest.mark.asyncio
    async def test_v1_idempotent(self, tmp_path):
        """Running migrate on already-v1 data should be a no-op."""
        p = str(tmp_path / "v1.jsonl")
        entry = TranscriptEntry.from_message(
            {"role": "user", "content": "hi"}, session_id="sid1"
        )
        with open(p, "w", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

        migrated = await migrate_transcript(p)
        assert migrated is False  # no migration needed

    @pytest.mark.asyncio
    async def test_empty_file_no_migration(self, tmp_path):
        p = str(tmp_path / "empty.jsonl")
        p_path = tmp_path / "empty.jsonl"
        p_path.write_text("")
        migrated = await migrate_transcript(p)
        assert migrated is False

    @pytest.mark.asyncio
    async def test_migrate_nonexistent_file(self, tmp_path):
        p = str(tmp_path / "nope.jsonl")
        migrated = await migrate_transcript(p)
        assert migrated is False

    @pytest.mark.asyncio
    async def test_partial_v1_with_message_key(self, tmp_path):
        """Entries that have 'message' key but no 'uuid' get wrapped properly."""
        p = str(tmp_path / "partial.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "message": {"role": "user", "content": "hi"},
                "sessionId": "sid1",
            }) + "\n")

        migrated = await migrate_transcript(p)
        # Should detect as v0 (no uuid) and migrate
        assert migrated is True
        entries = await load_entries(p)
        assert len(entries) == 1
        assert entries[0].message["role"] == "user"
