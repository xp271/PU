"""WebSearch built-in tool — DuckDuckGo web search via the ``ddgs`` library.

Uses :func:`asyncio.to_thread` to wrap the synchronous ``ddgs`` calls so
the Agent turn loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ._net import DEFAULT_TIMEOUT
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool

logger = logging.getLogger("agent_core.tools.web_search")

# ---------------------------------------------------------------------------
# Lazy availability check — ddgs is a hard dependency in pyproject.toml,
# but this guard gives a clear error at call-time if it's missing.
# ---------------------------------------------------------------------------
_DDGS_AVAILABLE = False
_DDGS_IMPORT_ERROR: str | None = None
try:
    from ddgs import DDGS  # noqa: F401
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException  # noqa: F401

    _DDGS_AVAILABLE = True
except ImportError as exc:
    _DDGS_IMPORT_ERROR = str(exc)
    logger.warning("ddgs not available — WebSearch will be a no-op: %s", exc)


SearchType = Literal["text", "images", "videos", "news"]
# ddgs 的合法 safesearch 取值是 on / moderate / off（'strict' 不被识别，会被静默忽略）。
SafeSearchLevel = Literal["on", "moderate", "off"]
TimeRange = Literal["d", "w", "m", "y"]


class WebSearchInput(BaseModel):
    query: str = Field(description="搜索查询字符串")
    max_results: int = Field(
        default=10, ge=1, le=30, description="最大返回结果数"
    )
    search_type: SearchType = Field(
        default="text", description="搜索类型: text(网页), images(图片), videos(视频), news(新闻)"
    )
    region: str = Field(
        default="us-en", description="区域代码，如 cn-zh, us-en, uk-en"
    )
    safe_search: SafeSearchLevel = Field(
        default="moderate", description="安全搜索级别: on(最严), moderate, off"
    )
    time_range: TimeRange | None = Field(
        default=None, description="时间范围过滤: d(天), w(周), m(月), y(年)"
    )


@register_tool
class WebSearchTool(Tool):
    name: ClassVar[str] = "WebSearch"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False   # 方案 A：不绕过权限询问
    is_destructive: ClassVar[bool] = False

    _MAX_OUTPUT_BYTES: ClassVar[int] = 100_000

    # ---- Tool ABC --------------------------------------------------------

    def validate_input(self, input: dict[str, Any]) -> WebSearchInput:
        return WebSearchInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return WebSearchInput.model_json_schema()

    async def call(
        self, input: WebSearchInput, context: ToolUseContext
    ) -> ToolCallResult:
        if not _DDGS_AVAILABLE:
            return ToolCallResult(
                content=f"WebSearch 不可用：ddgs 库未安装 ({_DDGS_IMPORT_ERROR})。"
                        f"请运行: uv sync",
                is_error=True,
            )

        try:
            results = await asyncio.to_thread(
                _search_sync,
                query=input.query,
                search_type=input.search_type,
                region=input.region,
                safesearch=input.safe_search,
                timelimit=input.time_range,
                max_results=input.max_results,
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as exc:
            return ToolCallResult(
                content=f"WebSearch 执行失败: {exc}",
                is_error=True,
            )

        if not results:
            return ToolCallResult(content=f"未找到相关结果 (query: {input.query})")

        # Format output
        lines: list[str] = [
            f'## Web Search Results for: "{input.query}"',
            f"(共 {len(results)} 条结果)",
            "",
        ]
        for i, r in enumerate(results, 1):
            lines.extend(_format_result(i, r))
            lines.append("")

        result_text = "\n".join(lines)

        # Truncate to byte cap
        output_bytes = result_text.encode("utf-8")
        if len(output_bytes) > self._MAX_OUTPUT_BYTES:
            truncated = output_bytes[: self._MAX_OUTPUT_BYTES]
            result_text = truncated.decode("utf-8", errors="replace")
            last_nl = result_text.rfind("\n")
            if last_nl > 0:
                result_text = result_text[:last_nl]
            return ToolCallResult(
                content=result_text,
                truncated=True,
                truncation_notice=f"结果已截断 (超过 {self._MAX_OUTPUT_BYTES} 字节)",
            )

        return ToolCallResult(content=result_text)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_result(index: int, r: dict[str, Any]) -> list[str]:
    """Format one ddgs result, tolerating the differing keys each search type
    returns.

    Key map across search types / backends:
      - text  → title, href, body
      - news  → title, url,  body / image
      - images→ title, image, url(=源页面)
      - videos→ title, content(=视频链接), description
    """
    title = r.get("title") or "(无标题)"
    # Link: text uses 'href'; news/images use 'url'; videos use 'content'.
    url = r.get("href") or r.get("url") or r.get("content") or ""
    # Snippet: text/news use 'body'; videos use 'description'.
    snippet = r.get("body") or r.get("description") or r.get("excerpt") or r.get("snippet") or ""
    # Direct media link for images.
    image = r.get("image") or ""

    out = [f"[{index}] {title}"]
    if url:
        out.append(f"    URL: {url}")
    if image and image != url:
        out.append(f"    图片: {image}")
    if snippet:
        out.append(f"    摘要: {snippet}")
    return out


# ---------------------------------------------------------------------------
# Synchronous search (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _search_sync(
    *,
    query: str,
    search_type: str,
    region: str,
    safesearch: str,
    timelimit: str | None,
    max_results: int,
    timeout: float,
) -> list[dict[str, Any]]:
    """Execute a ddgs search — must run in a worker thread.

    *ddgs* is already verified available before this is called; ``DDGS`` and the
    exception types are module-level imports (see top of file), which keeps them
    patchable in tests.
    """
    try:
        with DDGS(timeout=int(timeout)) as ddgs:
            method = getattr(ddgs, search_type)
            results = method(
                query,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=max_results,
            )
            return list(results)
    except RatelimitException:
        # Simple backoff retry (ddgs multi-backend auto-fallback)
        import time
        time.sleep(1.0)
        try:
            with DDGS(timeout=int(timeout)) as ddgs:
                method = getattr(ddgs, search_type)
                results = method(
                    query,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    max_results=max_results,
                )
                return list(results)
        except Exception as exc:
            raise RuntimeError(f"搜索服务暂不可用 (rate-limited): {exc}") from exc
    except TimeoutException:
        raise RuntimeError(f"搜索超时 ({timeout}s)") from None
    except DDGSException as exc:
        raise RuntimeError(f"搜索服务暂不可用: {exc}") from exc
