"""
DeepSeek Chat Completions streaming-API client.

Translates the DeepSeek API's streaming chunk stream (OpenAI-compatible)
into the provider-agnostic ``InternalEvent`` stream consumed by the agent
query loop.

Self-contained: all DeepSeek-specific logic (message conversion, tool
schemas, stop-reason mapping, error classification) lives here.

Key differences from OpenAI
---------------------------
- Default ``base_url`` is ``https://api.deepseek.com``.
- HTTP 402 (balance exhausted) is treated as ``fatal`` (not retryable).
- ``StreamStart.provider`` is ``"deepseek"``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import openai

from ..models import ModelProvider
from .base import BaseClient, register_client
from .errors import ErrorCategory
from .events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from ..tools.registry import registered_tools


# ---------------------------------------------------------------------------
# DeepSeek-specific constants
# ---------------------------------------------------------------------------

_DEEPSEEK_STOP_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_turns",
    "tool_calls": "end_turn",
    "content_filter": "permission_denied",
}

_DEFAULT_BASE_URL = "https://api.deepseek.com"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@register_client
class DeepSeekClient(BaseClient):
    """Streaming client backed by the DeepSeek Chat Completions API."""

    provider: ClassVar[ModelProvider] = ModelProvider.DEEPSEEK

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None,
    ) -> None:
        super().__init__(model, api_key, base_url)
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        kwargs["base_url"] = base_url or _DEFAULT_BASE_URL
        self._client = openai.AsyncOpenAI(**kwargs)

    # ------------------------------------------------------------------
    # Provider-specific class methods
    # ------------------------------------------------------------------

    @classmethod
    def convert_messages(
        cls,
        messages: list[dict[str, Any]],
        system: str | None,
    ) -> list[dict[str, Any]]:
        """Convert internal Anthropic content-block messages to OpenAI format."""
        openai_messages: list[dict[str, Any]] = []

        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", [])

            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
                continue

            if role == "user":
                text_parts: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_results.append(block)
                    else:
                        text_parts.append(block)

                if text_parts:
                    openai_messages.append({
                        "role": "user",
                        "content": cls._blocks_to_text(text_parts),
                    })

                for tr in tool_results:
                    result_content = tr.get("content", "")
                    if isinstance(result_content, (list, dict)):
                        result_content = json.dumps(result_content, ensure_ascii=False)
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content": str(result_content),
                    })

            elif role == "assistant":
                text_content = ""
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if block.get("type") == "text":
                        text_content += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"], ensure_ascii=False),
                            },
                        })

                msg_out: dict[str, Any] = {"role": "assistant"}
                msg_out["content"] = text_content or None
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                openai_messages.append(msg_out)

            else:
                openai_messages.append({"role": role, "content": content})

        return openai_messages

    @classmethod
    def convert_tools(
        cls,
        allowed_tools: list[str],
        registry: Any = None,
    ) -> list[dict[str, Any]]:
        """Generate DeepSeek Chat Completions API ``tools`` parameter format.

        *registry* is an optional ``ToolRegistry`` instance.  When
        ``None`` the module-level default is used (backward compat).
        """
        source = registry.registered_tools() if registry is not None else registered_tools()
        tools: list[dict[str, Any]] = []
        for name, tool_cls in source.items():
            if "*" not in allowed_tools and name not in allowed_tools:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (tool_cls.__doc__ or "").strip(),
                    "parameters": tool_cls.input_schema(),
                },
            })
        return tools

    @classmethod
    def normalize_stop_reason(cls, raw_reason: str) -> tuple[str, str | None]:
        """Map a DeepSeek finish_reason to internal StopReason."""
        mapped = _DEEPSEEK_STOP_MAP.get(raw_reason, "end_turn")
        detail = (
            f"deepseek finish_reason={raw_reason}"
            if raw_reason == "content_filter"
            else None
        )
        return mapped, detail

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Classify a DeepSeek (OpenAI SDK) exception."""
        if not isinstance(exception, openai.APIStatusError):
            return "transient"

        status = exception.status_code
        if status == 429:
            return "rate_limit"
        if status == 503:
            return "overloaded"
        if status in (500, 502):
            return "transient"
        if status == 402:
            return "fatal"  # balance exhausted, not retryable
        if status == 413 or (status == 400 and "context" in str(exception).lower()):
            return "context_length"
        if status == 400 and "content_filter" in str(exception).lower():
            return "content_filter"
        if status in (401, 403):
            return "fatal"
        return "fatal"

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        openai_messages = self.convert_messages(messages, system)

        yield StreamStart(
            turn_index=turn_index,
            provider="deepseek",
            model=self.model,
        )

        max_retries = 3
        base_delay = 4.0

        for attempt in range(max_retries + 1):
            try:
                async for event in self._do_stream(
                    openai_messages=openai_messages,
                    tools=tools,
                    max_tokens=max_tokens,
                ):
                    yield event
                return
            except openai.APIStatusError as e:
                category = self.classify_error(e)
                if category in ("rate_limit", "overloaded", "transient") and attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), 60)
                    await asyncio.sleep(delay)
                    continue
                yield ErrorMessage(
                    message=f"LLM error ({e.status_code}): {e.message}",
                    error_type=category,
                )
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
                return
            except Exception as e:
                category = self.classify_error(e)
                if category in ("rate_limit", "overloaded", "transient") and attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), 60)
                    await asyncio.sleep(delay)
                    continue
                yield ErrorMessage(message=str(e), error_type=category)
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
                return

    async def _do_stream(
        self,
        openai_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> AsyncGenerator[InternalEvent, None]:
        """Single-attempt streaming helper (retried by ``stream_messages``)."""
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            tools=tools or openai.NOT_GIVEN,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        current_tool_calls: dict[int, dict[str, Any]] = {}
        final_usage: Any = None
        final_finish_reason: str | None = None

        async for chunk in stream:
            if chunk.usage:
                final_usage = chunk.usage

            if chunk.choices and chunk.choices[0].finish_reason:
                final_finish_reason = chunk.choices[0].finish_reason

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta.content:
                yield AssistantText(content=delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in current_tool_calls:
                        current_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    tc = current_tool_calls[idx]
                    if tc_delta.id:
                        tc["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        tc["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        tc["arguments"] += tc_delta.function.arguments

        for idx in sorted(current_tool_calls):
            tc = current_tool_calls[idx]
            try:
                input_dict = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                input_dict = {"_raw": tc["arguments"]}
            yield ToolUse(
                tool_use_id=tc["id"],
                tool_name=tc["name"],
                tool_input=input_dict,
            )

        if final_usage:
            yield TokenCounts(
                input_tokens=final_usage.prompt_tokens,
                output_tokens=final_usage.completion_tokens,
            )

        yield StopEvent(
            stop_reason=final_finish_reason or "stop",
            finish_reason_detail=(
                f"deepseek finish_reason={final_finish_reason}"
                if final_finish_reason == "content_filter"
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
        """Convert Anthropic content blocks to a single text string."""
        parts: list[str] = []
        for block in blocks:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
