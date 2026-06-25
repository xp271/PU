"""
Configuration for the conversation logging / data-collection layer.

The whole layer is **opt-in** — :attr:`LoggingConfig.enabled` defaults to
``False`` so nothing is written to disk unless the caller explicitly turns it
on (invariant #3).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RollingPolicy = Literal["per_session", "per_day"]


class LoggingConfig(BaseModel):
    """Settings for the conversation log layer.  Disabled by default."""

    enabled: bool = Field(
        default=False,
        description="Enable conversation logging (opt-in — nothing is written when False)",
    )
    log_dir: str | None = Field(
        default=None,
        description="Log root directory.  None → ~/.agent/logs",
    )
    rolling: RollingPolicy = Field(
        default="per_session",
        description="File rolling policy: one file per session, or per calendar day",
    )

    # -- content scope (data collection vs metrics-only) --
    record_content: bool = Field(
        default=True,
        description=(
            "Record conversation text (prompts / assistant text / tool IO). "
            "When False only turn_metric / error / session_* records are kept "
            "— pure audit / observability mode."
        ),
    )
    record_tool_io: bool = Field(
        default=True,
        description="Record tool_input / tool_result content (metadata is always kept)",
    )

    # -- redaction / truncation --
    max_text_chars: int = Field(
        default=50_000,
        ge=0,
        description="Truncate text / content longer than this (0 = no truncation)",
    )
    redact_keys: list[str] = Field(
        default_factory=list,
        description="tool_input keys whose values are masked (e.g. 'api_key', 'password')",
    )

    # -- sampling --
    sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Per-session sampling rate (hashed on trace_id)",
    )

    # -- write behaviour --
    flush_every: int = Field(
        default=1,
        ge=1,
        description="Flush after every N records (1 = strongest durability)",
    )
    queue_maxsize: int = Field(
        default=10_000,
        ge=1,
        description="Background write queue cap; when full the oldest is dropped and counted",
    )


__all__ = ["LoggingConfig", "RollingPolicy"]
