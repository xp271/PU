"""Tests for bridge/text_splitter.py — 长文本拆分（QQ 1900 字符限制）。"""

from __future__ import annotations

from agent_core.bridge.text_splitter import split_text


class TestSplitText:
    """split_text 功能测试。"""

    def test_short_text_passthrough(self):
        assert split_text("你好") == ["你好"]

    def test_empty_string(self):
        assert split_text("") == []

    def test_exact_boundary(self):
        text = "a" * 1900
        assert split_text(text) == [text]

    def test_long_text_split(self):
        text = "a" * 3500
        result = split_text(text)
        assert len(result) == 2
        assert all(len(c) <= 1900 for c in result)

    def test_split_at_newline(self):
        text = "a" * 1800 + "\n" + "b" * 1800
        result = split_text(text)
        assert len(result) == 2
        assert result[0].endswith("a")

    def test_split_at_period(self):
        text = "a" * 1800 + "。rest" + "b" * 1800
        result = split_text(text)
        assert len(result) == 2

    def test_multi_chunk(self):
        text = "a" * 10000
        result = split_text(text)
        assert len(result) == 6
        assert all(len(c) <= 1900 for c in result)

    def test_custom_max_chars(self):
        text = "x" * 1200
        result = split_text(text, max_chars=500)
        assert len(result) == 3
        assert all(len(c) <= 500 for c in result)

    def test_split_fallback_hard_cut(self):
        text = "x" * 2500
        result = split_text(text)
        assert len(result) == 2
        assert result[0].endswith("x")

    def test_whitespace_only(self):
        assert split_text("   ") == []
