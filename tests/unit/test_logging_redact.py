"""Unit tests for redaction / truncation (Item 12)."""

from __future__ import annotations

import pytest

from agent_core.logging.config import LoggingConfig
from agent_core.logging.redact import Redactor


@pytest.mark.agent
class TestRedactor:
    def test_truncates_long_text_with_marker(self) -> None:
        r = Redactor(LoggingConfig(max_text_chars=5))
        out = r.text("abcdefghij")
        assert out.startswith("abcde")
        assert "+5 chars" in out

    def test_no_truncation_when_zero(self) -> None:
        r = Redactor(LoggingConfig(max_text_chars=0))
        s = "x" * 1000
        assert r.text(s) == s

    def test_masks_redacted_keys(self) -> None:
        r = Redactor(LoggingConfig(redact_keys=["password", "API_KEY"]))
        out = r.tool_input({"password": "secret", "API_key": "k", "cmd": "ls"})
        assert out["password"] == "***"
        assert out["API_key"] == "***"  # case-insensitive
        assert out["cmd"] == "ls"

    def test_nested_dict_scrub_does_not_mutate_original(self) -> None:
        r = Redactor(LoggingConfig(redact_keys=["token"]))
        original = {"outer": {"token": "abc", "ok": "v"}}
        out = r.tool_input(original)
        assert out["outer"]["token"] == "***"
        assert original["outer"]["token"] == "abc"  # untouched

    def test_truncates_long_values_in_tool_input(self) -> None:
        r = Redactor(LoggingConfig(max_text_chars=3))
        out = r.tool_input({"cmd": "abcdefgh"})
        assert out["cmd"].startswith("abc")
        assert "chars" in out["cmd"]

    def test_tool_result_non_string_passthrough(self) -> None:
        r = Redactor(LoggingConfig(max_text_chars=2))
        assert r.tool_result({"k": "v"}) == {"k": "v"}
        assert r.tool_result("abcdef").startswith("ab")
