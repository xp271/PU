"""
OpenAI Chat Completions streaming-API client.

Translates the OpenAI SDK's streaming chunk stream into the provider-agnostic
``InternalEvent`` stream consumed by the agent query loop.

Key differences from Anthropic
------------------------------
- Tool calls are accumulated across chunks via ``index`` keys.
- ``usage`` arrives in the final chunk (requires ``stream_options={"include_usage": True}``).
- ``finish_reason`` can signal ``content_filter`` which we map to a
  ``permission_denied`` stop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import openai
from openai.types.chat import ChatCompletionChunk

from .base import BaseClient
from .errors import classify_error
from .events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from .normalize import messages_for_openai


class OpenAIClient(BaseClient):
    """Streaming client backed by the OpenAI Chat Completions API."""

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
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        openai_messages = messages_for_openai(messages, system)

        yield StreamStart(
            turn_index=turn_index,
            provider="openai",
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
                return  # success
            except openai.APIStatusError as e:
                category = classify_error(e, openai.OpenAI)
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
                category = classify_error(e, openai.OpenAI)
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
            # Collect usage (OpenAI sends it in the final chunk)
            if chunk.usage:
                final_usage = chunk.usage

            # Collect finish_reason
            if chunk.choices and chunk.choices[0].finish_reason:
                final_finish_reason = chunk.choices[0].finish_reason

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # Text delta
            if delta.content:
                yield AssistantText(content=delta.content)

            # Tool call deltas (accumulate by index)
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

        # Yield accumulated tool calls in index order
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

        # Token counts
        if final_usage:
            yield TokenCounts(
                input_tokens=final_usage.prompt_tokens,
                output_tokens=final_usage.completion_tokens,
            )

        # Stop event
        yield StopEvent(
            stop_reason=final_finish_reason or "stop",
            finish_reason_detail=(
                f"openai finish_reason={final_finish_reason}"
                if final_finish_reason == "content_filter"
                else None
            ),
        )
