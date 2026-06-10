"""
Anthropic Messages streaming-API client.

Translates the Anthropic SDK's event stream into the provider-agnostic
``InternalEvent`` stream consumed by the agent query loop.

Self-contained: all Anthropic-specific logic (message conversion, tool
schemas, stop-reason mapping, error classification) lives here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import anthropic
from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
)

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
# Anthropic-specific constants
# ---------------------------------------------------------------------------

_ANTHROPIC_STOP_MAP: dict[str, str] = {
    "end_turn": "end_turn",
    "max_tokens": "max_turns",
    "stop_sequence": "end_turn",
    "tool_use": "end_turn",
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@register_client
class AnthropicClient(BaseClient):
    """Streaming client backed by the Anthropic Messages API."""

    provider: ClassVar[ModelProvider] = ModelProvider.ANTHROPIC

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

    # ------------------------------------------------------------------
    # Provider-specific class methods
    # ------------------------------------------------------------------

    @classmethod
    def convert_messages(
        cls,
        messages: list[dict[str, Any]],
        system: str | None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic Messages API parameters."""
        clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        return system, clean

    @classmethod
    def convert_tools(cls, allowed_tools: list[str]) -> list[dict[str, Any]]:
        """Generate Anthropic Messages API ``tools`` parameter format."""
        tools: list[dict[str, Any]] = []
        for name, tool_cls in registered_tools().items():
            if "*" not in allowed_tools and name not in allowed_tools:
                continue
            tools.append({
                "name": name,
                "description": (tool_cls.__doc__ or "").strip(),
                "input_schema": tool_cls.input_schema(),
            })
        return tools

    @classmethod
    def normalize_stop_reason(cls, raw_reason: str) -> tuple[str, str | None]:
        """Map an Anthropic stop reason to internal StopReason."""
        return _ANTHROPIC_STOP_MAP.get(raw_reason, "end_turn"), None

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Classify an Anthropic SDK exception."""
        if not isinstance(exception, anthropic.APIStatusError):
            return "transient"

        status = exception.status_code
        if status == 429:
            return "rate_limit"
        if status == 529:
            return "overloaded"
        if status == 413 or (status == 400 and "context" in str(exception).lower()):
            return "context_length"
        if status in (500, 502, 503):
            return "transient"
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
        system_param, clean_messages = self.convert_messages(messages, system)

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
                return
            except anthropic.APIStatusError as e:
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
                            pass
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
                        pass

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
