"""Unit tests for persistence/recovery.py."""

import pytest

from agent_core.persistence.recovery import (
    RecoveryResult,
    TurnInterruption,
    HistorySegment,
    clean_and_rebuild,
    detect_turn_interruption,
    filter_orphaned_thinking,
    filter_unresolved_tool_uses,
    reconstruct_full_history,
    reconstruct_live_messages,
    reconstruct_messages,
    walk_entry_chain,
)
from agent_core.persistence.storage import CompactionRecord, TranscriptEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _me(role, text, session_id="sid1"):
    """Make a TranscriptEntry with a simple text message."""
    return TranscriptEntry.from_message(
        {"role": role, "content": [{"type": "text", "text": text}]},
        session_id=session_id,
    )


def _tu_entry(role, tool_blocks, session_id="sid1"):
    """Make a TranscriptEntry with tool_use or tool_result blocks."""
    return TranscriptEntry.from_message(
        {"role": role, "content": tool_blocks},
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# walk_entry_chain
# ---------------------------------------------------------------------------

class TestWalkEntryChain:
    def test_linear_chain(self):
        e0 = TranscriptEntry(uuid="u0", parentUuid=None, sessionId="s", timestamp="t")
        e1 = TranscriptEntry(uuid="u1", parentUuid="u0", sessionId="s", timestamp="t")
        e2 = TranscriptEntry(uuid="u2", parentUuid="u1", sessionId="s", timestamp="t")
        chain = walk_entry_chain([e0, e1, e2])
        assert [e.uuid for e in chain] == ["u0", "u1", "u2"]

    def test_single_entry(self):
        e = TranscriptEntry(uuid="u0", parentUuid=None, sessionId="s", timestamp="t")
        chain = walk_entry_chain([e])
        assert len(chain) == 1

    def test_empty(self):
        assert walk_entry_chain([]) == []


# ---------------------------------------------------------------------------
# filter_orphaned_thinking
# ---------------------------------------------------------------------------

class TestFilterOrphanedThinking:
    def test_pure_thinking_removed(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
            {"role": "user", "content": [{"type": "text", "text": "bye"}]},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 2  # thinking message removed

    def test_thinking_with_tool_use_preserved(self):
        """Problem #8: thinking + tool_use must NOT be removed."""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "tool_use", "id": "tu1", "name": "Read", "input": {}},
                ],
            },
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 2  # preserved

    def test_thinking_with_text_preserved(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "hello"},
            ]},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 1

    def test_empty_assistant_removed(self):
        msgs = [
            {"role": "assistant", "content": []},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 0

    def test_non_assistant_untouched(self):
        msgs = [
            {"role": "user", "content": [{"type": "thinking", "thinking": "x"}]},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 1

    def test_redacted_thinking_only_removed(self):
        msgs = [
            {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "z"}]},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 0

    def test_thinking_with_whitespace_only_text_removed(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "   \n  "},
            ]},
        ]
        result = filter_orphaned_thinking(msgs)
        assert len(result) == 0  # whitespace text is not "meaningful"


# ---------------------------------------------------------------------------
# detect_turn_interruption
# ---------------------------------------------------------------------------

class TestDetectTurnInterruption:
    def test_normal_end_not_interrupted(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        r = detect_turn_interruption(msgs)
        assert not r.interrupted

    def test_last_is_user_prompt(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": "another prompt"}]},
        ]
        r = detect_turn_interruption(msgs)
        assert r.interrupted
        assert "user prompt" in r.detail

    def test_last_user_is_tool_result_not_interrupted(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "do it"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]},
        ]
        r = detect_turn_interruption(msgs)
        assert not r.interrupted

    def test_pending_tool_uses_detected(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "do it"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {}}]},
        ]
        r = detect_turn_interruption(msgs)
        assert r.interrupted
        assert "tu1" in r.pending_tool_uses

    def test_empty_messages(self):
        r = detect_turn_interruption([])
        assert not r.interrupted


# ---------------------------------------------------------------------------
# reconstruct_live_messages
# ---------------------------------------------------------------------------

class TestReconstructLiveMessages:
    def test_no_checkpoints_concatenates_all(self, tmp_path):
        ents = [
            _me("user", "hi"),
            _me("assistant", "hello"),
            _me("user", "bye"),
        ]
        live = reconstruct_live_messages(ents)
        assert len(live) == 3
        assert live[0]["content"][0]["text"] == "hi"

    def test_single_checkpoint_resets_base(self, tmp_path):
        # 3 pre-compaction messages, then checkpoint, then 2 post
        ents = [
            _me("user", "m1"),
            _me("assistant", "m2"),
            _me("user", "m3"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=1,
                    snapshot=[{"role": "user", "content": [{"type": "text", "text": "summary"}]}],
                    summary_count=1,
                    kept_count=0,
                ),
                session_id="sid1",
            ),
            _me("user", "post1"),
            _me("assistant", "post2"),
        ]
        live = reconstruct_live_messages(ents)
        # LIVE = checkpoint.snapshot + post entries
        assert len(live) == 3
        assert live[0]["content"][0]["text"] == "summary"
        assert live[1]["content"][0]["text"] == "post1"

    def test_multiple_checkpoints(self, tmp_path):
        ents = [
            _me("user", "s0_m1"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=1,
                    snapshot=[{"role": "user", "content": [{"type": "text", "text": "s1"}]}],
                    summary_count=1, kept_count=0,
                ), session_id="sid1",
            ),
            _me("user", "post1"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=2,
                    snapshot=[{"role": "user", "content": [{"type": "text", "text": "s2"}]}],
                    summary_count=1, kept_count=0,
                ), session_id="sid1",
            ),
            _me("user", "post2"),
        ]
        live = reconstruct_live_messages(ents)
        # LIVE = last checkpoint snapshot + post2
        assert len(live) == 2
        assert live[0]["content"][0]["text"] == "s2"
        assert live[1]["content"][0]["text"] == "post2"

    def test_checkpoint_with_kept_tail(self):
        """Snapshot carries summary + kept tail; LIVE = snapshot + post."""
        ents = [
            _me("user", "old1"),
            _me("assistant", "old2"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=1,
                    snapshot=[
                        {"role": "user", "content": [{"type": "text", "text": "SUM"}]},
                        {"role": "assistant", "content": [{"type": "text", "text": "old2"}]},
                    ],
                    summary_count=1,
                    kept_count=1,
                ),
                session_id="sid1",
            ),
            _me("user", "new"),
        ]
        live = reconstruct_live_messages(ents)
        assert [m["content"][0]["text"] for m in live] == ["SUM", "old2", "new"]

    def test_empty_list(self):
        assert reconstruct_live_messages([]) == []

    def test_filters_sidechain_entries(self):
        ents = [
            _me("user", "main"),
            TranscriptEntry.from_message(
                {"role": "user", "content": [{"type": "text", "text": "side"}]},
                session_id="sid1",
                is_sidechain=True,
            ),
        ]
        live = reconstruct_live_messages(ents)
        assert len(live) == 1
        assert live[0]["content"][0]["text"] == "main"


# ---------------------------------------------------------------------------
# reconstruct_full_history
# ---------------------------------------------------------------------------

class TestReconstructFullHistory:
    def test_no_checkpoints_single_segment(self):
        ents = [_me("user", "m1"), _me("assistant", "m2")]
        segs = reconstruct_full_history(ents)
        assert len(segs) == 1
        assert segs[0].index == 0
        assert segs[0].compaction is None
        assert len(segs[0].messages) == 2

    def test_checkpoint_splits_segments(self):
        ents = [
            _me("user", "s0_m1"),
            _me("user", "s0_m2"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=1,
                    snapshot=[{"role": "user", "content": [{"type": "text", "text": "s"}]}],
                    summary_count=1, kept_count=0,
                ), session_id="sid1",
            ),
            _me("user", "s1_m1"),
        ]
        segs = reconstruct_full_history(ents)
        assert len(segs) == 2
        assert segs[0].index == 0
        assert segs[0].compaction is not None
        assert len(segs[0].messages) == 2
        assert segs[1].index == 1
        assert segs[1].compaction is None
        assert len(segs[1].messages) == 1

    def test_empty(self):
        assert reconstruct_full_history([]) == []


# ---------------------------------------------------------------------------
# clean_and_rebuild
# ---------------------------------------------------------------------------

class TestCleanAndRebuild:
    @pytest.mark.asyncio
    async def test_basic_flow(self):
        ents = [
            _me("user", "hi"),
            _me("assistant", "ok"),
        ]
        result = await clean_and_rebuild(ents)
        assert len(result.messages) == 2
        assert result.entries_loaded == 2
        assert result.entries_kept == 2
        assert result.tool_uses_removed == 0
        assert result.compaction_count == 0
        assert result.written_count == 2
        assert result.interruption is not None
        assert not result.interruption.interrupted

    @pytest.mark.asyncio
    async def test_written_count_equals_live_before_cleaning(self):
        """Problem #1: written_count must be pre-cleaning LIVE length."""
        ents = [
            _me("user", "hi"),
            _tu_entry("assistant", [
                {"type": "tool_use", "id": "tu1", "name": "Read", "input": {}}
            ]),
        ]
        result = await clean_and_rebuild(ents)
        # written_count = 2 (original), but entries_kept may be less after cleaning
        assert result.written_count == 2
        # tool_use without result gets removed
        assert result.entries_kept < 2
        assert result.tool_uses_removed > 0

    @pytest.mark.asyncio
    async def test_with_compaction_checkpoints(self):
        ents = [
            _me("user", "old"),
            TranscriptEntry.from_checkpoint(
                CompactionRecord(
                    segment_index=1,
                    snapshot=[{"role": "user", "content": [{"type": "text", "text": "sum"}]}],
                    summary_count=1, kept_count=0,
                ), session_id="sid1",
            ),
            _me("user", "new"),
        ]
        result = await clean_and_rebuild(ents)
        assert result.compaction_count == 1
        assert result.written_count == 2  # snapshot + new message

    @pytest.mark.asyncio
    async def test_want_full_populates_segments(self):
        ents = [_me("user", "m1"), _me("assistant", "m2")]
        result = await clean_and_rebuild(ents, want_full=True)
        assert result.full_segments is not None
        assert len(result.full_segments) == 1
