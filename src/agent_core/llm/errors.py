"""
Per-provider error classification and tenacity retry-configuration factory.

The query() loop catches provider SDK exceptions and routes them through
``classify_error()`` to decide whether to retry (rate_limit / overloaded /
transient) or abort (context_length / content_filter / fatal).
"""

from __future__ import annotations

from typing import Literal

from ..models import ModelProvider


# ---------------------------------------------------------------------------
# Error category
# ---------------------------------------------------------------------------

ErrorCategory = Literal[
    "rate_limit",       # 429 / rate_limit -> exponential back-off retry
    "overloaded",       # 529 (Anthropic) / 503 (OpenAI) -> exponential back-off retry
    "context_length",   # context_window_exceeded -> not recoverable
    "content_filter",   # content filter triggered (OpenAI content_filter)
    "transient",        # 500 / 502 / network -> exponential back-off retry
    "fatal",            # auth / param / other unrecoverable
]


# ---------------------------------------------------------------------------
# Per-provider classification
# ---------------------------------------------------------------------------

def classify_error(exception: Exception, provider: ModelProvider) -> ErrorCategory:
    """Classify a provider SDK exception into one of the six error categories."""
    if provider == ModelProvider.ANTHROPIC:
        from anthropic import APIStatusError

        if not isinstance(exception, APIStatusError):
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

    if provider == ModelProvider.OPENAI:
        from openai import APIStatusError

        if not isinstance(exception, APIStatusError):
            return "transient"

        status = exception.status_code
        if status == 429:
            return "rate_limit"
        if status == 503:
            return "overloaded"
        if status in (500, 502):
            return "transient"
        if status == 413 or (status == 400 and "context" in str(exception).lower()):
            return "context_length"
        if status == 400 and "content_filter" in str(exception).lower():
            return "content_filter"
        if status in (401, 403):
            return "fatal"
        return "fatal"

    return "fatal"


# ---------------------------------------------------------------------------
# Tenacity retry-config factory
# ---------------------------------------------------------------------------

def _retry_predicate(provider_str: str):
    """Build a tenacity ``retry`` predicate that only retries on recoverable errors."""
    provider = ModelProvider(provider_str)

    def _should_retry(exception: Exception) -> bool:
        category = classify_error(exception, provider)
        return category in ("rate_limit", "overloaded", "transient")

    return _should_retry


def retry_config_for(provider: str) -> dict:
    """Return kwargs suitable for ``tenacity.retry(**kwargs)``.

    The returned dict is differentiated by provider but currently uses the
    same sensible defaults across all providers (exponential back-off 4→60 s,
    max 3 attempts).
    """
    return {
        "retry": _retry_predicate(provider),
        "wait": "wait_exponential(multiplier=1, min=4, max=60)",
        "stop": "stop_after_attempt(3)",
        "reraise": True,
    }
