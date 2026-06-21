"""Mock LLM client for integration testing the query loop.

Produces controlled ``InternalEvent`` sequences without a real API key.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, ClassVar

from agent_core.llm.base import BaseClient
from agent_core.llm.errors import ErrorCategory
from agent_core.llm.events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from agent_core.models import ModelProvider


class MockClient(BaseClient):
    """A BaseClient that yields a predefined script of InternalEvents."""

    provider: ClassVar[ModelProvider] = ModelProvider.ANTHROPIC

    def __init__(self, script: list[InternalEvent], model: str = "mock-model"):
        super().__init__(model=model, api_key=None, base_url=None)
        self._script = script

    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        yield StreamStart(
            turn_index=turn_index,
            provider="mock",
            model=self.model,
        )
        for event in self._script:
            yield event
        # After yielding the script, the mock must also yield a StopEvent
        # if the script doesn't already end with one (handled by helpers).

    @classmethod
    def convert_messages(
        cls, messages: list[dict[str, Any]], system: str | None
    ) -> Any:
        return (system, messages)

    @classmethod
    def convert_tools(
        cls, allowed_tools: list[str], registry: Any = None
    ) -> list[dict[str, Any]]:
        return []

    @classmethod
    def normalize_stop_reason(cls, raw_reason: str) -> tuple[str, str | None]:
        return ("end_turn", None)

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        return "fatal"


def make_text_turn(text: str = "Hello", input_tokens: int = 50, output_tokens: int = 10):
    """A simple turn: text response, no tool use."""
    return [
        AssistantText(content=text),
        TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
        StopEvent(stop_reason="end_turn"),
    ]


def make_tool_use_turn(
    tool_name: str = "Read",
    tool_input: dict[str, Any] | None = None,
    text: str = "",
):
    """A turn with one tool use."""
    if tool_input is None:
        tool_input = {"file_path": "/tmp/test.txt"}
    events: list[InternalEvent] = []
    if text:
        events.append(AssistantText(content=text))
    events.append(
        ToolUse(
            tool_use_id="tu_mock_001",
            tool_name=tool_name,
            tool_input=tool_input,
        )
    )
    events.append(TokenCounts(input_tokens=100, output_tokens=20))
    events.append(StopEvent(stop_reason="tool_use"))
    return events


def make_error_turn(error_msg: str = "overloaded"):
    """A turn that fails with an error."""
    return [
        ErrorMessage(message=error_msg, error_type="overloaded"),
        StopEvent(stop_reason="error"),
    ]
