from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_core.tools import WebFetchTool, get_tool
from agent_core.tools.base import ToolUseContext

# Patch target for the SSRF guard inside web_fetch (so fetch/parse tests don't
# touch real DNS). Dedicated SSRF tests below exercise the real guard via
# IP-literal URLs, which never hit the network.
_SSRF = "agent_core.tools.web_fetch.assert_public_host"


def _make_response(
    *,
    status: int = 200,
    content_type: str = "text/html",
    body: bytes = b"",
    location: str | None = None,
    encoding: str | None = "utf-8",
) -> MagicMock:
    """Build a mock of a *streamed* httpx.Response."""
    resp = MagicMock()
    resp.status_code = status
    headers = {"content-type": content_type}
    if location is not None:
        headers["location"] = location
        resp.has_redirect_location = True
    else:
        resp.has_redirect_location = False
    resp.headers = headers
    resp.encoding = encoding

    async def _aiter():
        if body:
            yield body

    resp.aiter_bytes = _aiter
    resp.aclose = AsyncMock()
    return resp


def _make_client(*, responses=None, send_side_effect=None) -> MagicMock:
    """Mock httpx.AsyncClient. ``responses`` are returned by send() in order."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    client.build_request = MagicMock(return_value=MagicMock())
    if send_side_effect is not None:
        client.send = AsyncMock(side_effect=send_side_effect)
    else:
        client.send = AsyncMock(side_effect=list(responses or []))
    return client


@pytest.mark.web_fetch
class TestWebFetchTool:
    def tool(self) -> WebFetchTool:
        return get_tool("WebFetch")  # type: ignore[return-value]

    # ---- validate_input -------------------------------------------------

    def test_validate_input_minimal(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "https://example.com"})
        assert inp.url == "https://example.com"
        assert inp.max_length == 100_000

    def test_validate_input_full(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "https://example.com", "max_length": 5000})
        assert inp.url == "https://example.com"
        assert inp.max_length == 5000

    def test_validate_input_max_length_bounds(self) -> None:
        tool = self.tool()
        with pytest.raises(Exception):
            tool.validate_input({"url": "https://example.com", "max_length": 0})
        with pytest.raises(Exception):
            tool.validate_input({"url": "https://example.com", "max_length": 500_001})

    # ---- input_schema ---------------------------------------------------

    def test_input_schema(self) -> None:
        schema = WebFetchTool.input_schema()
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "url" in props
        assert "max_length" in props

    # ---- call: SSRF / scheme rejection (real guard, IP-literal = no DNS) -

    @pytest.mark.asyncio
    async def test_rejects_non_http_scheme(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "file:///etc/passwd"})
        result = await tool.call(inp, ToolUseContext())
        assert result.is_error
        assert "仅支持 http/https" in str(result.content)

    @pytest.mark.asyncio
    async def test_rejects_empty_scheme(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "localhost"})
        result = await tool.call(inp, ToolUseContext())
        assert result.is_error
        assert "仅支持 http/https" in str(result.content)

    @pytest.mark.asyncio
    async def test_rejects_loopback_ip(self) -> None:
        tool = self.tool()
        for bad_url in [
            "http://127.0.0.1:8080/admin",
            "http://127.0.0.1/",
            "http://[::1]/",
        ]:
            inp = tool.validate_input({"url": bad_url})
            result = await tool.call(inp, ToolUseContext())
            assert result.is_error, f"Should reject {bad_url}"
            assert "SSRF" in str(result.content), f"Should mention SSRF for {bad_url}"

    @pytest.mark.asyncio
    async def test_rejects_private_ip(self) -> None:
        tool = self.tool()
        for bad_url in [
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
        ]:
            inp = tool.validate_input({"url": bad_url})
            result = await tool.call(inp, ToolUseContext())
            assert result.is_error, f"Should reject {bad_url}"
            assert "SSRF" in str(result.content), f"Should mention SSRF for {bad_url}"

    @pytest.mark.asyncio
    async def test_rejects_cloud_metadata_ip(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "http://169.254.169.254/latest/meta-data/"})
        result = await tool.call(inp, ToolUseContext())
        assert result.is_error
        assert "SSRF" in str(result.content)

    @pytest.mark.asyncio
    async def test_rejects_invalid_url_format(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({"url": "not-a-url"})
        result = await tool.call(inp, ToolUseContext())
        assert result.is_error

    @pytest.mark.asyncio
    async def test_redirect_to_private_ip_is_blocked(self) -> None:
        """A public URL 302-ing to an internal address must be blocked: the
        redirect target is SSRF-revalidated before the next request."""
        tool = self.tool()
        # hop0: public IP literal (passes guard, no DNS) -> 302 to loopback
        redirect = _make_response(
            status=302, location="http://127.0.0.1/admin", body=b""
        )
        client = _make_client(responses=[redirect])
        with patch("httpx.AsyncClient", return_value=client):
            inp = tool.validate_input({"url": "http://93.184.216.34/"})
            result = await tool.call(inp, ToolUseContext())
        assert result.is_error
        assert "SSRF" in str(result.content)
        # Second hop must never have been sent.
        assert client.send.await_count == 1

    @pytest.mark.asyncio
    async def test_too_many_redirects(self) -> None:
        tool = self.tool()
        # Always redirect to another public IP literal -> exhausts the cap.
        loop_resp = _make_response(
            status=302, location="http://93.184.216.34/", body=b""
        )
        client = _make_client(send_side_effect=lambda *a, **k: loop_resp)
        with patch("httpx.AsyncClient", return_value=client):
            inp = tool.validate_input({"url": "http://93.184.216.34/"})
            result = await tool.call(inp, ToolUseContext())
        assert result.is_error
        assert "重定向次数过多" in str(result.content)

    # ---- call: happy path (SSRF guard patched out) ----------------------

    @pytest.mark.asyncio
    async def test_call_fetch_html(self) -> None:
        tool = self.tool()
        resp = _make_response(
            content_type="text/html; charset=utf-8",
            body=b"<html><body><h1>Hello</h1><p>World</p></body></html>",
        )
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        content = str(result.content)
        assert "Content fetched from: https://example.com" in content
        assert "Hello" in content
        assert not result.truncated

    @pytest.mark.asyncio
    async def test_call_fetch_plain_text(self) -> None:
        tool = self.tool()
        resp = _make_response(content_type="text/plain", body=b"plain text content")
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com/data.txt"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert "plain text content" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_follows_redirect_to_public(self) -> None:
        """A redirect to another *public* host is followed and the final URL is
        reported."""
        tool = self.tool()
        redirect = _make_response(
            status=301, location="https://example.org/final", body=b""
        )
        final = _make_response(content_type="text/plain", body=b"final body")
        client = _make_client(responses=[redirect, final])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        content = str(result.content)
        assert "final body" in content
        assert "example.org/final" in content
        assert client.send.await_count == 2

    @pytest.mark.asyncio
    async def test_html_to_markdown_fallback_on_error(self) -> None:
        """If html2text raises, fall back to tag-stripping rather than erroring."""
        tool = self.tool()
        resp = _make_response(
            content_type="text/html",
            body=b"<html><body><p>Fallback text</p></body></html>",
        )
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF), patch(
            "agent_core.tools.web_fetch._html_to_markdown",
            side_effect=RuntimeError("boom"),
        ):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert "Fallback text" in str(result.content)

    # ---- call: HTTP errors ----------------------------------------------

    @pytest.mark.asyncio
    async def test_call_http_404(self) -> None:
        tool = self.tool()
        resp = _make_response(status=404, content_type="text/html", body=b"not found")
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com/404"})
            result = await tool.call(inp, ToolUseContext())

        assert result.is_error
        assert "HTTP 404" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_timeout(self) -> None:
        tool = self.tool()
        client = _make_client(send_side_effect=httpx.TimeoutException("timeout"))
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert result.is_error
        assert "超时" in str(result.content)

    @pytest.mark.asyncio
    async def test_call_request_error(self) -> None:
        tool = self.tool()
        client = _make_client(
            send_side_effect=httpx.RequestError("connection refused")
        )
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert result.is_error
        assert "网络请求失败" in str(result.content)

    # ---- call: truncation -----------------------------------------------

    @pytest.mark.asyncio
    async def test_call_truncation_by_max_length(self) -> None:
        tool = self.tool()
        resp = _make_response(content_type="text/plain", body=b"A" * 2000)
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF):
            inp = tool.validate_input({"url": "https://example.com", "max_length": 100})
            result = await tool.call(inp, ToolUseContext())

        assert result.truncated
        assert result.truncation_notice is not None
        assert "字符" in result.truncation_notice
        content = str(result.content)
        assert len(content) <= 100 + 200  # allow header/footer overhead

    @pytest.mark.asyncio
    async def test_call_download_cap(self) -> None:
        """Body larger than the byte cap is stopped mid-stream (DoS guard)."""
        tool = self.tool()
        resp = _make_response(content_type="text/plain", body=b"B" * 5000)
        client = _make_client(responses=[resp])
        with patch("httpx.AsyncClient", return_value=client), patch(_SSRF), patch(
            "agent_core.tools.web_fetch._MAX_DOWNLOAD_BYTES", 100
        ):
            inp = tool.validate_input({"url": "https://example.com"})
            result = await tool.call(inp, ToolUseContext())

        assert not result.is_error
        assert result.truncated
        assert "字节上限" in str(result.truncation_notice)

    # ---- tool attributes ------------------------------------------------

    def test_tool_attributes(self) -> None:
        tool = self.tool()
        assert tool.name == "WebFetch"
        assert tool.is_concurrency_safe is False
        assert tool.is_read_only is False
        assert tool.is_destructive is False
