"""
Shared error types and retry-configuration utilities.

Per-provider error classification has moved into each ``BaseClient``
subclass's ``classify_error()`` class method.
"""

from __future__ import annotations

from typing import Literal


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
# Tenacity retry-config factory
# ---------------------------------------------------------------------------

def _retry_predicate(classify_error_fn, provider_str: str):
    """Build a tenacity ``retry`` predicate that uses a provider-specific
    ``classify_error`` callable."""

    def _should_retry(exception: Exception) -> bool:
        category = classify_error_fn(exception)
        return category in ("rate_limit", "overloaded", "transient")

    return _should_retry


def retry_config_for(classify_error_fn, provider: str) -> dict:
    """Return kwargs suitable for ``tenacity.retry(**kwargs)``.

    *classify_error_fn* is the provider's ``BaseClient.classify_error`` class
    method.  The returned dict uses exponential back-off 4→60 s, max 3
    attempts.
    """
    return {
        "retry": _retry_predicate(classify_error_fn, provider),
        "wait": "wait_exponential(multiplier=1, min=4, max=60)",
        "stop": "stop_after_attempt(3)",
        "reraise": True,
    }
