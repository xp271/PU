"""
Anthropic Messages streaming-API client.

Translates the Anthropic SDK's event stream into the provider-agnostic
``InternalEvent`` stream consumed by the agent query loop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import anthropic
from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
)

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
from .normalize import messages_for_anthropic


class AnthropicClient(BaseClient):
    """Streaming client backed by the Anthropic Messages API."""

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
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        system_param, clean_messages = messages_for_anthropic(messages, system)

        yield StreamStart(
            turn_index=turn_index,
            provider="anthropic",
            model=self.model,
        )

        max_retries = 3
        base_delay = 4.0

        for attempt in range(max_retries + 1):
            try:
                async for event in self._do_stream(
                    clean_messages=clean_messages,
                    tools=tools,
                    system=system_param,
                    max_tokens=max_tokens,
                ):
                    yield event
                return  # success — stream completed without errors
            except anthropic.APIStatusError as e:
                category = classify_error(e, anthropic.Anthropic)
                if category in ("rate_limit", "overloaded", "transient") and attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), 60)
                    await asyncio.sleep(delay)
                    continue
                # Fatal or retries exhausted
                yield ErrorMessage(
                    message=f"LLM error ({e.status_code}): {e.message}",
                    error_type=category,
                )
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
                return
            except Exception as e:
                category = classify_error(e, anthropic.Anthropic)
                if category in ("rate_limit", "overloaded", "transient") and attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), 60)
                    await asyncio.sleep(delay)
                    continue
                yield ErrorMessage(message=str(e), error_type=category)
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
                return

    async def _do_stream(
        self,
        clean_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None,
        max_tokens: int,
    ) -> AsyncGenerator[InternalEvent, None]:
        """Single-attempt streaming helper (retried by ``stream_messages``)."""
        async with self._client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            messages=clean_messages,
            system=system or anthropic.NOT_GIVEN,
            tools=tools or anthropic.NOT_GIVEN,
        ) as stream:
            current_tool_use: dict[str, Any] | None = None

            async for event in stream:
                match event:
                    case ContentBlockStartEvent():
                        if event.content_block.type == "text":
                            pass  # text accumulator not needed per-block
                        elif event.content_block.type == "tool_use":
                            current_tool_use = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "input_json": "",
                            }

                    case ContentBlockDeltaEvent() as delta:
                        if delta.delta.type == "text_delta":
                            yield AssistantText(content=delta.delta.text)
                        elif delta.delta.type == "input_json_delta":
                            if current_tool_use is not None:
                                current_tool_use["input_json"] += (
                                    delta.delta.partial_json
                                )

                    case ContentBlockStopEvent():
                        if current_tool_use is not None:
                            try:
                                input_dict = json.loads(
                                    current_tool_use["input_json"]
                                )
                            except json.JSONDecodeError:
                                input_dict = {
                                    "_raw": current_tool_use["input_json"]
                                }
                            yield ToolUse(
                                tool_use_id=current_tool_use["id"],
                                tool_name=current_tool_use["name"],
                                tool_input=input_dict,
                            )
                            current_tool_use = None

                    case MessageDeltaEvent():
                        pass  # usage info comes via MessageStopEvent

                    case MessageStopEvent():
                        usage = stream.current_message_snapshot.usage
                        yield TokenCounts(
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cache_read_tokens=getattr(
                                usage, "cache_read_input_tokens", None
                            ),
                            cache_write_tokens=getattr(
                                usage, "cache_creation_input_tokens", None
                            ),
                        )

                        stop_reason = (
                            stream.current_message_snapshot.stop_reason
                            or "end_turn"
                        )
                        yield StopEvent(
                            stop_reason=stop_reason,
                            finish_reason_detail=None,
                        )
