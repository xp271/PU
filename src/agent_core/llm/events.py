"""
Internal event types consumed by the agent query() loop.

These extend the public AgentMessage discriminated union with
provider-internal events (token counts, stream boundaries, stop signals).
The query() loop consumes only ``InternalEvent`` and is unaware of any
specific provider.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..models import AssistantText, ErrorMessage, ToolUse


class StreamStart(BaseModel):
    """Emitted at the very beginning of a stream, carrying turn metadata."""

    type: Literal["stream_start"] = "stream_start"
    turn_index: int
    provider: str
    model: str


class TokenCounts(BaseModel):
    """LLM token usage statistics for the current turn.

    This is an internal event — it is NOT exposed to consuming code via
    AgentMessage.  The DoneMessage at stream end carries the run-level totals.
    """

    type: Literal["token_counts"] = "token_counts"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None   # Anthropic prompt-caching only
    cache_write_tokens: int | None = None  # Anthropic prompt-caching only


class StopEvent(BaseModel):
    """Termination signal from the LLM stream.

    Consumed by query() and turned into a DoneMessage (or, when tool_use
    blocks are pending, the loop continues to the next turn).
    """

    type: Literal["stop"] = "stop"
    stop_reason: str                     # Provider-raw stop reason
    finish_reason_detail: str | None = None


# -- discriminated union consumed by query() --

InternalEvent = Annotated[
    StreamStart
    | AssistantText
    | ToolUse
    | TokenCounts
    | StopEvent
    | ErrorMessage,
    Field(discriminator="type"),
]
