"""Unit tests for hard_truncate()."""

from __future__ import annotations

from agent_core.query.truncate import hard_truncate


def _make_user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _make_assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class TestHardTruncate:
    def test_empty_messages(self) -> None:
        result = hard_truncate([], max_tokens=1000)
        assert result == []

    def test_single_round_under_limit(self) -> None:
        msgs = [_make_user("Hello"), _make_assistant("Hi there")]
        result = hard_truncate(msgs, max_tokens=1000)
        assert len(result) == 2
        assert result == msgs

    def test_multiple_rounds_under_limit(self) -> None:
        msgs = [
            _make_user("Round 1"),
            _make_assistant("Response 1"),
            _make_user("Round 2"),
            _make_assistant("Response 2"),
        ]
        result = hard_truncate(msgs, max_tokens=1000)
        assert len(result) == 4
        assert result == msgs

    def test_truncation_drops_oldest_rounds(self) -> None:
        # Two rounds, threshold so low only the most recent 1 fits
        msgs = [
            _make_user("A" * 200),       # ~50 tokens
            _make_assistant("B" * 200),  # ~50 tokens
            _make_user("C" * 200),       # ~50 tokens
            _make_assistant("D" * 200),  # ~50 tokens
        ]
        # 2 rounds × 200 chars = 100 tokens per round. With threshold 70,
        # only round 2 fits.
        result = hard_truncate(msgs, max_tokens=70, min_rounds=1)
        # Should have: truncation notice + round 2 (user + assistant)
        assert len(result) == 3  # notice + user + assistant
        assert result[0]["content"][0]["text"].startswith("<system-reminder>")
        assert "Earlier conversation history has been truncated" in result[0]["content"][0]["text"]
        assert result[1] == msgs[2]  # user from round 2
        assert result[2] == msgs[3]  # assistant from round 2

    def test_min_rounds_respected(self) -> None:
        """When min_rounds forces keeping rounds that exceed threshold,
        all rounds are kept unchanged (no truncation notice needed)."""
        msgs = [
            _make_user("A" * 500),       # ~125 tokens
            _make_assistant("B" * 500),  # ~125 tokens
            _make_user("C" * 500),       # ~125 tokens
            _make_assistant("D" * 500),  # ~125 tokens
        ]
        # Each round is ~250 tokens, threshold is 50 (below any single round)
        # but min_rounds=2 forces keeping both rounds → no truncation
        result = hard_truncate(msgs, max_tokens=50, min_rounds=2)
        # All messages preserved (nothing dropped → no notice)
        assert result == msgs
        assert len(result) == 4

    def test_no_truncation_when_no_rounds_to_drop(self) -> None:
        """When nothing exceeds the limit, return unchanged."""
        msgs = [
            _make_user("A"),
            _make_assistant("B"),
        ]
        result = hard_truncate(msgs, max_tokens=100_000)
        assert result == msgs

    def test_assistant_only_after_user_not_split(self) -> None:
        """Multiple assistant messages after a user stay in the same round."""
        big = "X" * 200  # ~50 tokens per message
        msgs = [
            _make_user(big),
            _make_assistant(big),
            _make_assistant(big),
            _make_user("Q2"),
            _make_assistant("A2"),
        ]
        # Round 1 has 3 messages (~150 tokens), round 2 has 2 (~50 tokens)
        # threshold of 60 drops round 1, keeps round 2
        result = hard_truncate(msgs, max_tokens=60, min_rounds=1)
        # Round 2 preserved: user Q2 + assistant A2, + notice
        assert len(result) == 3
        assert result[1]["content"][0]["text"] == "Q2"
        assert result[2]["content"][0]["text"] == "A2"

    def test_truncation_notice_includes_round_counts(self) -> None:
        msgs = [
            _make_user("Q" * 100),
            _make_assistant("A" * 100),
            _make_user("Q" * 100),
            _make_assistant("A" * 100),
            _make_user("Q" * 100),
            _make_assistant("A" * 100),
        ]
        result = hard_truncate(msgs, max_tokens=60, min_rounds=1)
        notice = result[0]["content"][0]["text"]
        # 1 round of 3 total preserved
        assert "1 of 3" in notice or "most recent 1" in notice
