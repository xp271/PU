"""WebFetch built-in tool — fetches a URL and returns its content as Markdown.

Uses ``httpx.AsyncClient`` (non-blocking) with SSRF host validation.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from ._net import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, assert_public_host
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool

# Cap on redirects we will follow manually (each hop is SSRF-revalidated).
_MAX_REDIRECTS = 5
# Hard cap on bytes read from the network, independent of max_length (which
# bounds the *returned* text). Guards against a server streaming gigabytes.
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024


class WebFetchInput(BaseModel):
    url: str = Field(description="要抓取内容的目标 URL（仅 http/https）")
    max_length: int = Field(
        default=100_000, ge=1, le=500_000,
        description="返回内容的最大字符数（超出截断）",
    )


@register_tool
class WebFetchTool(Tool):
    name: ClassVar[str] = "WebFetch"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False   # 方案 A：不绕过权限询问
    is_destructive: ClassVar[bool] = False

    # ---- Tool ABC --------------------------------------------------------

    def validate_input(self, input: dict[str, Any]) -> WebFetchInput:
        return WebFetchInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return WebFetchInput.model_json_schema()

    async def call(
        self, input: WebFetchInput, context: ToolUseContext
    ) -> ToolCallResult:
        import httpx

        current = input.url.strip()
        final_url = current
        content_type = ""
        body = ""

        # Manual redirect handling: every hop (including the original URL) is
        # SSRF-revalidated *before* the request, which closes the redirect-based
        # SSRF bypass (a public URL 302-ing to 169.254.169.254 etc.).
        #
        # Residual: a same-hostname DNS-rebinding attacker could still pass
        # assert_public_host() and have httpx re-resolve to a private IP within
        # one request's TOCTOU window. Fully closing that needs IP pinning with
        # SNI override, which httpx 0.28 does not expose cleanly. Documented as
        # a known limitation.
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                # 1. scheme + SSRF validation for this hop
                try:
                    assert_public_host(current)
                except ValueError as exc:
                    return ToolCallResult(content=str(exc), is_error=True)

                # 2. Send (stream so we can cap the download size)
                try:
                    request = client.build_request("GET", current)
                    response = await client.send(request, stream=True)
                except httpx.TimeoutException:
                    return ToolCallResult(
                        content=f"请求超时 ({DEFAULT_TIMEOUT}s): {current}",
                        is_error=True,
                    )
                except httpx.RequestError as exc:
                    return ToolCallResult(
                        content=f"网络请求失败: {exc}", is_error=True
                    )

                try:
                    # 3. Follow redirects manually (re-validated next loop)
                    if response.has_redirect_location:
                        location = response.headers.get("location", "")
                        current = urljoin(current, location)
                        continue

                    # 4. Final response — check status
                    if response.status_code != 200:
                        return ToolCallResult(
                            content=f"请求失败 HTTP {response.status_code}: {current}",
                            is_error=True,
                        )

                    final_url = current
                    content_type = response.headers.get("content-type", "")
                    body, download_capped = await _read_capped(response)
                    break
                finally:
                    await response.aclose()
            else:
                return ToolCallResult(
                    content=f"重定向次数过多 (>{_MAX_REDIRECTS}): {input.url}",
                    is_error=True,
                )

        # 5. HTML → Markdown conversion (only for HTML pages)
        if "text/html" in content_type:
            try:
                body = _html_to_markdown(body)
            except Exception:
                # Fallback: strip tags
                body = _strip_html_tags(body)

        # 6. Truncate returned text to max_length
        char_truncated = len(body) > input.max_length
        if char_truncated:
            body = body[:input.max_length]
            # Cut to last newline boundary
            last_nl = body.rfind("\n")
            if last_nl > 0:
                body = body[:last_nl]
        truncated = char_truncated or download_capped

        # 7. Format output
        header = f"## Content fetched from: {final_url}\n"
        footer = (
            f"\n\n---\n(内容长度: {len(body):,} 字符, "
            f"截断: {'是' if truncated else '否'})"
        )

        result_text = header + body + footer

        if char_truncated:
            notice = f"内容已截断到 {input.max_length:,} 字符"
        elif download_capped:
            notice = f"下载超过 {_MAX_DOWNLOAD_BYTES:,} 字节上限，已截断"
        else:
            notice = None

        return ToolCallResult(
            content=result_text,
            truncated=truncated,
            truncation_notice=notice,
        )


# ---------------------------------------------------------------------------
# Network read helper
# ---------------------------------------------------------------------------

async def _read_capped(response: Any) -> tuple[str, bool]:
    """Read a streamed httpx response body, stopping at _MAX_DOWNLOAD_BYTES.

    Returns ``(text, hit_cap)`` where *hit_cap* is True if the download was
    truncated. Decodes with the response's declared encoding (falling back to
    UTF-8) so a multi-gigabyte body can never be loaded whole into memory.
    """
    buf = bytearray()
    hit_cap = False
    async for chunk in response.aiter_bytes():
        buf.extend(chunk)
        if len(buf) >= _MAX_DOWNLOAD_BYTES:
            del buf[_MAX_DOWNLOAD_BYTES:]
            hit_cap = True
            break
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        return bytes(buf).decode(encoding, errors="replace"), hit_cap
    except (LookupError, TypeError):
        return bytes(buf).decode("utf-8", errors="replace"), hit_cap


# ---------------------------------------------------------------------------
# HTML processing helpers
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"<[^>]*>")


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags, collapse whitespace (fallback when html2text fails)."""
    text = _STRIP_RE.sub(" ", html)
    # Collapse multiple whitespace/newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown using html2text."""
    import html2text

    h = html2text.HTML2Text()
    h.body_width = 0          # no line wrapping
    h.ignore_links = False
    h.ignore_images = False
    h.ignore_emphasis = False
    h.skip_internal_links = True
    h.single_line_break = False
    return h.handle(html).strip()
