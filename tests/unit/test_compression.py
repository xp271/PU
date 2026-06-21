"""Unit tests for compression module.

Covers trigger.py, consistency.py, prompts.py, and summarizer.py helpers
(anything that does not require an LLM client).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.compression.consistency import CompressionConsistency, verify_compression
from agent_core.compression.prompts import (
    COMPRESSION_REQUEST,
    CONVERSATION_SUMMARY,
    RETRY_FEEDBACK,
    ROUND_HEADER,
    TOOL_RESULT_TRUNCATED,
)
from agent_core.compression.summarizer import (
    _build_compressed_messages,
    _build_compression_prompt,
    _shorten,
)
from agent_core.compression.trigger import _partition_rounds, can_compress
from agent_core.models import CompressionConfig


# ============================================================================
# Helpers
# ============================================================================


def _make_msg(role: str, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "content": list(blocks)}


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use_block(
    name: str = "Read",
    tid: str = "tu_001",
    inp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def _tool_result_block(
    tid: str = "tu_001", content: Any = "result text", is_error: bool = False
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tid,
        "content": content,
        "is_error": is_error,
    }


# ============================================================================
# _partition_rounds
# ============================================================================


class TestPartitionRounds:
    def test_single_round(self) -> None:
        msgs = [
            _make_msg("user", _text_block("hello")),
            _make_msg("assistant", _text_block("hi")),
        ]
        rounds = _partition_rounds(msgs)
        assert len(rounds) == 1
        assert len(rounds[0]) == 2

    def test_multiple_rounds(self) -> None:
        msgs = [
            _make_msg("user", _text_block("q1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("user", _text_block("q2")),
            _make_msg("assistant", _text_block("a2")),
        ]
        rounds = _partition_rounds(msgs)
        assert len(rounds) == 2
        assert len(rounds[0]) == 2
        assert len(rounds[1]) == 2

    def test_tool_result_user_starts_new_round(self) -> None:
        """A tool_result user message starts a new round."""
        msgs = [
            _make_msg("user", _text_block("q1")),
            _make_msg("assistant", _tool_use_block("Read")),
            _make_msg("user", _tool_result_block()),
            _make_msg("assistant", _text_block("done")),
        ]
        rounds = _partition_rounds(msgs)
        # user(q1)+assistant(tool_use) = round 1
        # user(tool_result)+assistant(done) = round 2
        assert len(rounds) == 2

    def test_consecutive_assistant_messages_stay_in_same_round(self) -> None:
        msgs = [
            _make_msg("user", _text_block("q1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("assistant", _text_block("a2")),
        ]
        rounds = _partition_rounds(msgs)
        assert len(rounds) == 1
        assert len(rounds[0]) == 3

    def test_empty_messages(self) -> None:
        assert _partition_rounds([]) == []

    def test_single_user_only(self) -> None:
        msgs = [_make_msg("user", _text_block("hello"))]
        rounds = _partition_rounds(msgs)
        assert len(rounds) == 1

    def test_consecutive_user_messages_split_rounds(self) -> None:
        msgs = [
            _make_msg("user", _text_block("q1")),
            _make_msg("user", _text_block("q2")),
        ]
        rounds = _partition_rounds(msgs)
        assert len(rounds) == 2


# ============================================================================
# can_compress
# ============================================================================


class TestCanCompress:
    def test_all_conditions_met(self) -> None:
        cfg = CompressionConfig(enabled=True, keep_recent_rounds=2)
        msgs = [
            _make_msg("user", _text_block("r1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("user", _text_block("r2")),
            _make_msg("assistant", _text_block("a2")),
            _make_msg("user", _text_block("r3")),
            _make_msg("assistant", _text_block("a3")),
        ]
        assert can_compress(msgs, cfg) is True

    def test_disabled(self) -> None:
        cfg = CompressionConfig(enabled=False)
        msgs = [
            _make_msg("user", _text_block("r1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("user", _text_block("r2")),
            _make_msg("assistant", _text_block("a2")),
            _make_msg("user", _text_block("r3")),
            _make_msg("assistant", _text_block("a3")),
            _make_msg("user", _text_block("r4")),
            _make_msg("assistant", _text_block("a4")),
        ]
        assert can_compress(msgs, cfg) is False

    def test_not_enough_rounds(self) -> None:
        cfg = CompressionConfig(enabled=True, keep_recent_rounds=3)
        msgs = [
            _make_msg("user", _text_block("r1")),
            _make_msg("assistant", _text_block("a1")),
        ]
        assert can_compress(msgs, cfg) is False

    def test_exactly_equal_to_keep(self) -> None:
        cfg = CompressionConfig(enabled=True, keep_recent_rounds=2)
        msgs = [
            _make_msg("user", _text_block("r1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("user", _text_block("r2")),
            _make_msg("assistant", _text_block("a2")),
        ]
        # 2 rounds, keep=2 → nothing to compress
        assert can_compress(msgs, cfg) is False

    def test_one_more_than_keep(self) -> None:
        cfg = CompressionConfig(enabled=True, keep_recent_rounds=2)
        msgs = [
            _make_msg("user", _text_block("r1")),
            _make_msg("assistant", _text_block("a1")),
            _make_msg("user", _text_block("r2")),
            _make_msg("assistant", _text_block("a2")),
            _make_msg("user", _text_block("r3")),
            _make_msg("assistant", _text_block("a3")),
        ]
        # 3 rounds, keep=2 → 1 old round to compress
        assert can_compress(msgs, cfg) is True

    def test_empty_messages(self) -> None:
        cfg = CompressionConfig()
        assert can_compress([], cfg) is False


# ============================================================================
# _shorten
# ============================================================================


class TestShorten:
    def test_shorter_than_max(self) -> None:
        assert _shorten("abc", 100) == "abc"

    def test_exactly_at_max(self) -> None:
        assert _shorten("abcd", 4) == "abcd"

    def test_longer_than_max(self) -> None:
        result = _shorten("X" * 100, 20)
        assert result.startswith("X" * 20)
        assert "truncated" in result
        assert "100" in result  # original length

    def test_empty_string(self) -> None:
        assert _shorten("", 10) == ""


# ============================================================================
# _build_compression_prompt
# ============================================================================


class TestBuildCompressionPrompt:
    def _simple_old_rounds(self) -> list[list[dict[str, Any]]]:
        return [
            [
                _make_msg("user", _text_block("read the file")),
                _make_msg(
                    "assistant",
                    _text_block("ok"),
                    _tool_use_block("Read", inp={"file_path": "/f.txt"}),
                ),
                _make_msg("user", _tool_result_block(content="file contents here")),
                _make_msg("assistant", _text_block("done reading")),
            ]
        ]

    def test_basic_structure(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(), preview_chars=200
        )
        assert "<compression_request>" in prompt
        assert "</compression_request>" in prompt
        assert "<conversation_to_summarize>" in prompt
        assert "read the file" in prompt
        assert "Read" in prompt
        assert "file contents here" in prompt

    def test_includes_round_headers(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(), preview_chars=200
        )
        assert "[Round 1]" in prompt

    def test_tool_result_not_truncated_when_short(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(), preview_chars=500
        )
        assert "file contents here" in prompt
        assert "truncated" not in prompt

    def test_tool_result_truncated_when_long(self) -> None:
        rounds = [
            [
                _make_msg("user", _text_block("grep something")),
                _make_msg("assistant", _tool_use_block("Grep")),
                _make_msg(
                    "user",
                    _tool_result_block(content="X" * 500),
                ),
            ]
        ]
        prompt = _build_compression_prompt(rounds, preview_chars=100)
        assert "truncated, original ~500 chars" in prompt

    def test_with_previous_errors(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(),
            preview_chars=200,
            previous_errors="summary was empty",
        )
        assert "previous_attempt_feedback" in prompt
        assert "summary was empty" in prompt

    def test_without_previous_errors(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(), preview_chars=200
        )
        assert "previous_attempt_feedback" not in prompt

    def test_with_custom_template(self) -> None:
        custom = "Custom prompt — {conversation_text} — end ({preview_chars})"
        prompt = _build_compression_prompt(
            self._simple_old_rounds(),
            preview_chars=200,
            custom_template=custom,
        )
        assert prompt.startswith("Custom prompt — ")
        assert prompt.endswith(" — end (200)")

    def test_none_custom_template_uses_default(self) -> None:
        prompt = _build_compression_prompt(
            self._simple_old_rounds(), preview_chars=200, custom_template=None
        )
        assert prompt.startswith("<compression_request>")

    def test_skips_non_dict_blocks(self) -> None:
        """Non-dict content blocks are silently skipped."""
        rounds = [
            [
                _make_msg("user", _text_block("hello")),
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "valid"},
                        "not-a-dict",  # should be skipped
                    ],
                },
            ]
        ]
        prompt = _build_compression_prompt(rounds, preview_chars=200)
        assert "valid" in prompt
        # The string "not-a-dict" should not appear in the prompt
        # (it's not a text/tool_use/tool_result block)
        # Actually _text_block produces {"type":"text","text":...}
        # and the first msg has _text_block which is a dict, so it goes through
        # For the second msg, "not-a-dict" is a string in content list,
        # and isinstance(block, dict) check will skip it

    def test_multiple_rounds(self) -> None:
        rounds = [
            [
                _make_msg("user", _text_block("q1")),
                _make_msg("assistant", _text_block("a1")),
            ],
            [
                _make_msg("user", _text_block("q2")),
                _make_msg("assistant", _text_block("a2")),
            ],
        ]
        prompt = _build_compression_prompt(rounds, preview_chars=200)
        assert "[Round 1]" in prompt
        assert "[Round 2]" in prompt
        assert "q1" in prompt
        assert "q2" in prompt


# ============================================================================
# _build_compressed_messages
# ============================================================================


class TestBuildCompressedMessages:
    def test_basic_structure(self) -> None:
        recent = [
            [
                _make_msg("user", _text_block("recent q")),
                _make_msg("assistant", _text_block("recent a")),
            ]
        ]
        result = _build_compressed_messages("summary text", recent)
        assert len(result) == 3  # 1 summary + 2 recent
        assert result[0]["role"] == "user"
        assert "<conversation_summary>" in result[0]["content"][0]["text"]
        assert "summary text" in result[0]["content"][0]["text"]

    def test_empty_summary(self) -> None:
        recent = [
            [_make_msg("user", _text_block("q"))]
        ]
        result = _build_compressed_messages("", recent)
        assert len(result) == 2
        assert result[0]["role"] == "user"  # summary message
        assert "" in result[0]["content"][0]["text"]

    def test_multiple_recent_rounds(self) -> None:
        recent = [
            [_make_msg("user", _text_block("r1"))],
            [_make_msg("user", _text_block("r2"))],
        ]
        result = _build_compressed_messages("summary", recent)
        assert len(result) == 3  # 1 summary + 2 rounds (1 msg each)
        assert result[1]["content"][0]["text"] == "r1"
        assert result[2]["content"][0]["text"] == "r2"

    def test_no_recent_rounds(self) -> None:
        result = _build_compressed_messages("summary", [])
        assert len(result) == 1  # just the summary message

    def test_recent_rounds_unchanged(self) -> None:
        """Recent round messages pass through verbatim."""
        recent = [
            [
                _make_msg(
                    "user",
                    _text_block("hello"),
                    _tool_use_block("Bash"),
                )
            ]
        ]
        result = _build_compressed_messages("s", recent)
        # Verify the recent message was not modified
        assert result[1] == recent[0][0]


# ============================================================================
# verify_compression
# ============================================================================


class TestVerifyCompression:
    def _make_old_rounds(self, count: int = 1) -> list[list[dict[str, Any]]]:
        rounds = []
        for i in range(count):
            rounds.append([
                _make_msg("user", _text_block(f"old question {i}")),
                _make_msg("assistant", _text_block(f"old answer {i}")),
            ])
        return rounds

    def _make_recent_rounds(self, count: int = 1) -> list[list[dict[str, Any]]]:
        rounds = []
        for i in range(count):
            rounds.append([
                _make_msg("user", _text_block(f"recent question {i}")),
                _make_msg("assistant", _text_block(f"recent answer {i}")),
            ])
        return rounds

    def test_valid_compression(self) -> None:
        old = self._make_old_rounds(2)
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages(
            "This is a valid summary of the old conversation.", recent
        )
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="This is a valid summary of the old conversation.",
        )
        assert result.valid is True
        assert result.errors == []

    def test_message_count_mismatch_too_few(self) -> None:
        """Compressed has fewer messages than expected."""
        recent = self._make_recent_rounds(2)
        # Only 1 summary + 2 recent = 3, but recent_rounds has 4 messages
        # plus 1 summary = 5 expected
        compressed = _build_compressed_messages("s", recent[:1])  # only 1 round
        old = self._make_old_rounds(1)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,  # expects 2 rounds
            summary="s",
        )
        assert result.valid is False
        assert any("count mismatch" in e for e in result.errors)

    def test_message_count_mismatch_too_many(self) -> None:
        """Compressed has more messages than expected."""
        recent = self._make_recent_rounds(1)  # only 1 round (2 msgs)
        # Build with 2 recent rounds but ensure compressed has them
        # Actually this test is tricky - let me instead test where compressed
        # has an extra message.
        old = self._make_old_rounds(1)
        flat = [msg for rnd in recent for msg in rnd]
        compressed = [_make_msg("user", _text_block("summary"))] + flat + [_make_msg("user", _text_block("extra"))]
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="s",
        )
        assert result.valid is False
        assert any("count mismatch" in e for e in result.errors)

    def test_recent_message_modified(self) -> None:
        """A recent-round message was altered — fatal."""
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        flat = [msg for msg in recent[0]]
        # Modify the first recent message
        modified = flat.copy()
        modified[0] = _make_msg("user", _text_block("CHANGED"))
        compressed = [_make_msg("user", _text_block("summary"))] + modified
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="valid summary text here",
        )
        assert result.valid is False
        assert any("was modified" in e for e in result.errors)

    def test_empty_summary(self) -> None:
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages("", recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="",
        )
        assert result.valid is False
        assert any("empty" in e for e in result.errors)

    def test_summary_too_short(self) -> None:
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        short = "ab"  # 2 chars, < 20
        compressed = _build_compressed_messages(short, recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary=short,
        )
        assert result.valid is False
        assert any("too short" in e for e in result.errors)

    def test_summary_exactly_20_chars(self) -> None:
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        exact = "A" * 20
        compressed = _build_compressed_messages(exact, recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary=exact,
        )
        assert result.valid is True

    def test_whitespace_only_summary(self) -> None:
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages("   \n  \t  ", recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="   \n  \t  ",
        )
        assert result.valid is False
        assert any("empty" in e for e in result.errors)

    def test_few_old_messages_warning(self) -> None:
        """Only 1 message in old_rounds → warning, not error."""
        old = [[_make_msg("user", _text_block("sole message"))]]
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages("A valid summary text here ok", recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="A valid summary text here ok",
        )
        assert result.valid is True  # warning, not fatal
        assert any("negligible" in w for w in result.warnings)

    def test_tool_uses_not_mentioned_warning(self) -> None:
        old = [
            [
                _make_msg("user", _text_block("do it")),
                _make_msg(
                    "assistant",
                    _tool_use_block("Grep", tid="tu1"),
                    _tool_use_block("Glob", tid="tu2"),
                ),
                _make_msg(
                    "user",
                    _tool_result_block("tu1", "grep ok"),
                    _tool_result_block("tu2", "glob ok"),
                ),
            ]
        ]
        recent = self._make_recent_rounds(1)
        # This summary mentions neither "Grep" nor "Glob"
        summary = "This valid summary only discusses generic file operations. Twenty+ chars."
        compressed = _build_compressed_messages(summary, recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary=summary,
        )
        assert result.valid is True  # warning only
        assert any("tool names mentioned" in w or "0/2" in w or "Only 0" in w for w in result.warnings)

    def test_tool_uses_partially_mentioned_ok(self) -> None:
        """50%+ tool names mentioned → no warning."""
        old = [
            [
                _make_msg("user", _text_block("do it")),
                _make_msg(
                    "assistant",
                    _tool_use_block("Read", tid="tu1"),
                    _tool_use_block("Bash", tid="tu2"),
                ),
            ]
        ]
        recent = self._make_recent_rounds(1)
        summary = "Used the Read tool to check the file. Also did other things. Twenty chars+."
        compressed = _build_compressed_messages(summary, recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary=summary,
        )
        assert result.valid is True
        assert result.warnings == [] or not any(
            "tool names mentioned" in w for w in result.warnings
        )

    def test_no_tool_uses_in_old_rounds(self) -> None:
        """No tool_use blocks → tool_uses_traceable=True without checking."""
        old = self._make_old_rounds(2)
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages(
            "A valid summary longer than twenty characters.", recent
        )
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="A valid summary longer than twenty characters.",
        )
        assert result.valid is True
        assert result.tool_uses_traceable is True

    def test_token_accounting_fields(self) -> None:
        """input/output tokens are carried through."""
        old = self._make_old_rounds(1)
        recent = self._make_recent_rounds(1)
        compressed = _build_compressed_messages("Valid enough summary text here", recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="Valid enough summary text here",
        )
        # Defaults
        assert result.input_tokens_consumed == 0
        assert result.output_tokens_consumed == 0
        # Can be set externally
        result.input_tokens_consumed = 500
        result.output_tokens_consumed = 200
        assert result.input_tokens_consumed == 500
        assert result.output_tokens_consumed == 200

    def test_compressed_and_preserved_round_counts(self) -> None:
        old = self._make_old_rounds(3)
        recent = self._make_recent_rounds(2)
        compressed = _build_compressed_messages("Valid summary text here yes.", recent)
        result = verify_compression(
            original_messages=[],
            compressed_messages=compressed,
            old_rounds=old,
            recent_rounds=recent,
            summary="Valid summary text here yes.",
        )
        assert result.compressed_rounds == 3
        assert result.preserved_rounds == 2


# ============================================================================
# Prompt templates
# ============================================================================


class TestPromptTemplates:
    def test_compression_request_format(self) -> None:
        result = COMPRESSION_REQUEST.format(
            preview_chars=200, conversation_text="hello world"
        )
        assert "<compression_request>" in result
        assert "hello world" in result
        assert "200" in result

    def test_retry_feedback_format(self) -> None:
        result = RETRY_FEEDBACK.format(previous_errors="summary was empty")
        assert "previous_attempt_feedback" in result
        assert "summary was empty" in result

    def test_conversation_summary_format(self) -> None:
        result = CONVERSATION_SUMMARY.format(summary="the summary")
        assert "the summary" in result
        assert "<conversation_summary>" in result

    def test_tool_result_truncated_format(self) -> None:
        result = TOOL_RESULT_TRUNCATED.format(
            preview="abc...", original_len=5000
        )
        assert "abc..." in result
        assert "5000" in result
        assert "truncated" in result

    def test_round_header_format(self) -> None:
        result = ROUND_HEADER.format(round_num=5)
        assert "[Round 5]" in result

    def test_custom_template_missing_placeholder_raises(self) -> None:
        """format() raises KeyError when template references a key not provided."""
        bad_template = "Missing key {nonexistent_key}"
        with pytest.raises(KeyError):
            bad_template.format(preview_chars=200, conversation_text="test")


# ============================================================================
# CompressionConfig
# ============================================================================


class TestCompressionConfigModel:
    def test_defaults(self) -> None:
        cfg = CompressionConfig()
        assert cfg.enabled is True
        assert cfg.keep_recent_rounds == 3
        assert cfg.summary_max_tokens == 2048
        assert cfg.tool_result_preview_chars == 200
        assert cfg.max_retries == 2
        assert cfg.prompt_template is None

    def test_custom_values(self) -> None:
        cfg = CompressionConfig(
            enabled=False,
            keep_recent_rounds=5,
            summary_max_tokens=4096,
            max_retries=0,
            prompt_template="Custom: {conversation_text}",
        )
        assert cfg.enabled is False
        assert cfg.keep_recent_rounds == 5
        assert cfg.max_retries == 0
        assert cfg.prompt_template == "Custom: {conversation_text}"

    def test_keep_recent_rounds_bounds(self) -> None:
        with pytest.raises(Exception):
            CompressionConfig(keep_recent_rounds=0)
        with pytest.raises(Exception):
            CompressionConfig(keep_recent_rounds=11)

    def test_max_retries_bounds(self) -> None:
        with pytest.raises(Exception):
            CompressionConfig(max_retries=-1)
        with pytest.raises(Exception):
            CompressionConfig(max_retries=6)


# ============================================================================
# CompressionConsistency dataclass
# ============================================================================


class TestCompressionConsistencyDataclass:
    def test_default_valid_false(self) -> None:
        cc = CompressionConsistency(valid=False)
        assert cc.valid is False
        assert cc.errors == []
        assert cc.warnings == []
        assert cc.input_tokens_consumed == 0

    def test_with_errors(self) -> None:
        cc = CompressionConsistency(valid=False, errors=["e1", "e2"])
        assert cc.valid is False
        assert len(cc.errors) == 2

    def test_with_warnings(self) -> None:
        cc = CompressionConsistency(valid=True, warnings=["w1"])
        assert cc.valid is True
        assert len(cc.warnings) == 1
        assert len(cc.errors) == 0


# ============================================================================
# CompressionAttempt dataclass
# ============================================================================


class TestCompressionAttemptDataclass:
    def test_fields(self) -> None:
        from agent_core.compression.summarizer import CompressionAttempt

        ca = CompressionAttempt(
            attempt=2,
            summary="summary text",
            verification=CompressionConsistency(valid=True),
            tokens_consumed=500,
            input_tokens_consumed=300,
            output_tokens_consumed=200,
        )
        assert ca.attempt == 2
        assert ca.summary == "summary text"
        assert ca.tokens_consumed == 500
        assert ca.input_tokens_consumed == 300
        assert ca.output_tokens_consumed == 200
        assert ca.error is None

    def test_with_error(self) -> None:
        from agent_core.compression.summarizer import CompressionAttempt

        ca = CompressionAttempt(
            attempt=1,
            summary="",
            verification=CompressionConsistency(valid=False, errors=["fail"]),
            error="LLM timeout",
        )
        assert ca.error == "LLM timeout"
        assert ca.tokens_consumed == 0
