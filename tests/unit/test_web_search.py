from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_core.tools import WebSearchTool, get_tool
from agent_core.tools.base import ToolUseContext


@pytest.mark.web_search
class TestWebSearchTool:
    def tool(self) -> WebSearchTool:
        return get_tool("WebSearch")  # type: ignore[return-value]

    # ---- validate_input -------------------------------------------------

    def test_validate_input_minimal(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"query": "Python 3.12 release"})
        assert inp.query == "Python 3.12 release"
        assert inp.max_results == 10
        assert inp.search_type == "text"
        assert inp.region == "us-en"
        assert inp.safe_search == "moderate"
        assert inp.time_range is None

    def test_validate_input_full(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({
            "query": "AI news",
            "max_results": 5,
            "search_type": "news",
            "region": "cn-zh",
            "safe_search": "on",
            "time_range": "w",
        })
        assert inp.query == "AI news"
        assert inp.max_results == 5
        assert inp.search_type == "news"
        assert inp.region == "cn-zh"
        assert inp.safe_search == "on"
        assert inp.time_range == "w"

    def test_validate_input_rejects_invalid_safe_search(self) -> None:
        tool = self.tool()
        # 'strict' is NOT a valid ddgs safesearch level (on/moderate/off).
        with pytest.raises(Exception):
            tool.validate_input({"query": "test", "safe_search": "strict"})

    def test_validate_input_rejects_invalid_search_type(self) -> None:
        tool = self.tool()
        with pytest.raises(Exception):
            tool.validate_input({"query": "test", "search_type": "invalid"})

    def test_validate_input_rejects_invalid_time_range(self) -> None:
        tool = self.tool()
        with pytest.raises(Exception):
            tool.validate_input({"query": "test", "time_range": "x"})

    def test_validate_input_max_results_bounds(self) -> None:
        tool = self.tool()
        with pytest.raises(Exception):
            tool.validate_input({"query": "test", "max_results": 0})
        with pytest.raises(Exception):
            tool.validate_input({"query": "test", "max_results": 31})

    # ---- input_schema ---------------------------------------------------

    def test_input_schema(self) -> None:
        schema = WebSearchTool.input_schema()
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "query" in props
        assert "max_results" in props
        assert "search_type" in props
        assert "region" in props
        assert "safe_search" in props
        assert "time_range" in props

    # ---- call: happy path -----------------------------------------------

    @pytest.mark.asyncio
    async def test_call_text_search(self) -> None:
        tool = self.tool()
        mock_results = [
            {"title": "Python 3.12", "href": "https://python.org", "body": "New release"},
            {"title": "Python Docs", "href": "https://docs.python.org", "body": "Documentation"},
        ]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "Python 3.12"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        content = str(result.content)
        assert "Python 3.12" in content
        assert "python.org" in content
        assert "(共 2 条结果)" in content

    @pytest.mark.asyncio
    async def test_call_no_results(self) -> None:
        tool = self.tool()
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=[]
        ):
            inp = tool.validate_input({"query": "xyzzy_no_results_test"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert "未找到相关结果" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_search_exception(self) -> None:
        tool = self.tool()
        with patch(
            "agent_core.tools.web_search._search_sync",
            side_effect=RuntimeError("搜索超时 (15s)"),
        ):
            inp = tool.validate_input({"query": "test"})
            result = await tool.call(inp, ToolUseContext())

        assert result.is_error
        assert "搜索超时" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_images_search_type(self) -> None:
        tool = self.tool()
        mock_results = [
            {"title": "cat.jpg", "url": "https://example.com/cat.jpg", "body": ""},
        ]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "cats", "search_type": "images"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert "cat.jpg" in str(result.content)

    @pytest.mark.asyncio
    async def test_result_without_href_falls_back_to_url(self) -> None:
        tool = self.tool()
        mock_results = [
            {"title": "No href", "url": "https://example.com", "body": "desc"},
        ]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "test"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert "https://example.com" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_videos_field_mapping(self) -> None:
        """videos results carry link in 'content' and text in 'description' —
        these must not be silently dropped."""
        tool = self.tool()
        mock_results = [
            {
                "title": "Cute cats compilation",
                "content": "https://video.example.com/watch?v=abc",
                "description": "10 minutes of cats",
            },
        ]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "cats", "search_type": "videos"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        content = str(result.content)
        assert "Cute cats compilation" in content
        assert "https://video.example.com/watch?v=abc" in content  # link not dropped
        assert "10 minutes of cats" in content                      # desc not dropped

    @pytest.mark.asyncio
    async def test_call_news_field_mapping(self) -> None:
        """news results use 'url' + 'body' and may carry an 'image'."""
        tool = self.tool()
        mock_results = [
            {
                "title": "Breaking",
                "url": "https://news.example.com/story",
                "body": "Something happened",
                "image": "https://news.example.com/img.jpg",
            },
        ]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "news", "search_type": "news"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        content = str(result.content)
        assert "https://news.example.com/story" in content
        assert "Something happened" in content
        assert "https://news.example.com/img.jpg" in content

    @pytest.mark.asyncio
    async def test_call_when_ddgs_unavailable(self) -> None:
        """Graceful error (not a crash) when the ddgs library is missing."""
        tool = self.tool()
        with patch("agent_core.tools.web_search._DDGS_AVAILABLE", False), patch(
            "agent_core.tools.web_search._DDGS_IMPORT_ERROR", "No module named 'ddgs'"
        ):
            inp = tool.validate_input({"query": "test"})
            result = await tool.call(inp, ToolUseContext())

        assert result.is_error
        assert "ddgs" in str(result.content)

    def test_rate_limit_retry_then_succeed(self) -> None:
        """_search_sync retries once after a RatelimitException."""
        import agent_core.tools.web_search as ws

        calls = {"n": 0}
        good = [{"title": "ok", "href": "https://a.com", "body": "b"}]

        class _DDGS:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ws.RatelimitException("slow down")
                return good

        with patch.object(ws, "DDGS", _DDGS), patch("time.sleep"):
            out = ws._search_sync(
                query="q", search_type="text", region="us-en",
                safesearch="moderate", timelimit=None, max_results=5, timeout=15,
            )
        assert out == good
        assert calls["n"] == 2

    def test_rate_limit_retry_then_fail(self) -> None:
        import agent_core.tools.web_search as ws

        class _DDGS:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, *a, **k):
                raise ws.RatelimitException("still limited")

        with patch.object(ws, "DDGS", _DDGS), patch("time.sleep"):
            with pytest.raises(RuntimeError, match="rate-limited"):
                ws._search_sync(
                    query="q", search_type="text", region="us-en",
                    safesearch="moderate", timelimit=None, max_results=5, timeout=15,
                )

    # ---- truncation -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_call_truncation_on_large_result(self) -> None:
        tool = self.tool()
        # 1 result with a very long snippet to trigger byte cap
        long_body = "x" * 120_000
        mock_results = [{"title": "Test", "href": "https://a.com", "body": long_body}]
        with patch(
            "agent_core.tools.web_search._search_sync", return_value=mock_results
        ):
            inp = tool.validate_input({"query": "test"})
            result = await tool.call(inp, ToolUseContext())

        assert result.truncated
        assert result.truncation_notice is not None

    # ---- tool attributes ------------------------------------------------

    def test_tool_attributes(self) -> None:
        tool = self.tool()
        assert tool.name == "WebSearch"
        assert tool.is_concurrency_safe is False
        assert tool.is_read_only is False
        assert tool.is_destructive is False
