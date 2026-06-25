"""
Log record data models for the conversation logging / data-collection layer.

Each line of the JSONL log file is exactly one :class:`LogRecord` — a Pydantic
discriminated union (on ``kind``) that spans **both** conversation content
(user prompts, assistant text, tool calls/results) and runtime metrics
(per-turn token / latency / provider).  A shared envelope carries the
correlation fields (``trace_id`` / ``session_id`` / ``turn_index``) so the two
data sources can be joined during analysis.

This layer is intentionally *separate* from ``persistence`` (Item 10): the
envelope fields are deliberately named differently (``kind`` event stream here
vs ``message`` / ``compaction`` recovery semantics there) so the two file
families are never mistaken for one another.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """ISO 8601 timestamp in UTC with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Shared envelope
# ---------------------------------------------------------------------------


class _LogEnvelope(BaseModel):
    """Fields common to every log record."""

    ts: str = Field(default_factory=utc_now_iso)
    """ISO 8601 (UTC, millisecond) capture time."""

    seq: int = -1
    """Monotonic per-session sequence number.  Assigned by the sink on
    ``emit`` (``-1`` is a placeholder filled in at write time), giving a total
    order even when two records share a ``ts`` and enabling dedup / resume."""

    trace_id: str
    """``Agent._trace_id`` — spans one Agent lifetime."""

    session_id: str | None = None
    """``SessionState.session_id`` (``None`` for sessionless runs)."""

    project_hash: str
    """``compute_project_hash(workspace_root)``."""

    turn_index: int | None = None
    """Turn this record belongs to (best effort for content events)."""

    agent_depth: int = 0
    """0 = main agent; >0 = sub-agent (sidechain)."""

    is_sidechain: bool = False
    """True when the record originates from a sub-agent."""


# ---------------------------------------------------------------------------
# Record variants (discriminated on ``kind``)
# ---------------------------------------------------------------------------


class SessionStartRecord(_LogEnvelope):
    kind: Literal["session_start"] = "session_start"
    model: str
    permission_mode: str
    workspace_root: str | None = None
    resumed: bool = False


class UserPromptRecord(_LogEnvelope):
    kind: Literal["user_prompt"] = "user_prompt"
    text: str


class AssistantTextRecord(_LogEnvelope):
    kind: Literal["assistant_text"] = "assistant_text"
    text: str


class ToolUseRecord(_LogEnvelope):
    kind: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)


class ToolResultRecord(_LogEnvelope):
    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any = None
    is_error: bool = False
    truncated: bool = False


class TurnMetricRecord(_LogEnvelope):
    kind: Literal["turn_metric"] = "turn_metric"
    provider: str = ""
    model: str = ""
    status: Literal["success", "error"] = "success"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    tool_uses: int = 0
    tool_names: list[str] = Field(default_factory=list)
    latency_ms: float | None = None
    error_type: str | None = None


class ErrorRecord(_LogEnvelope):
    kind: Literal["error"] = "error"
    message: str
    error_type: str | None = None


class SessionEndRecord(_LogEnvelope):
    kind: Literal["session_end"] = "session_end"
    stop_reason: str
    total_tokens: int = 0
    tool_uses_total: int = 0
    error_detail: str | None = None


LogRecord = Annotated[
    SessionStartRecord
    | UserPromptRecord
    | AssistantTextRecord
    | ToolUseRecord
    | ToolResultRecord
    | TurnMetricRecord
    | ErrorRecord
    | SessionEndRecord,
    Field(discriminator="kind"),
]


# A bare Pydantic adapter for parsing arbitrary log lines back into the union.
from pydantic import TypeAdapter  # noqa: E402

LogRecordAdapter: TypeAdapter[Any] = TypeAdapter(LogRecord)


__all__ = [
    "utc_now_iso",
    "SessionStartRecord",
    "UserPromptRecord",
    "AssistantTextRecord",
    "ToolUseRecord",
    "ToolResultRecord",
    "TurnMetricRecord",
    "ErrorRecord",
    "SessionEndRecord",
    "LogRecord",
    "LogRecordAdapter",
]
