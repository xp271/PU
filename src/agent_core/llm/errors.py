"""
Shared error types and retry-configuration utilities.

Per-provider error classification has moved into each ``BaseClient``
subclass's ``classify_error()`` class method.
"""

from __future__ import annotations

from typing import Literal

from tenacity import retry_if_exception, stop_after_attempt, wait_exponential


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
    attempts and reraises the final exception.

    NOTE: the concrete provider clients currently implement retry inline in
    their ``stream_messages`` loops (so they can yield ``ErrorMessage`` /
    ``StopEvent`` instead of raising).  This factory is kept as a public,
    *functional* utility — previously its ``wait``/``stop`` were placeholder
    strings that would have raised if actually passed to ``tenacity.retry``.
    """
    return {
        "retry": retry_if_exception(_retry_predicate(classify_error_fn, provider)),
        "wait": wait_exponential(multiplier=1, min=4, max=60),
        "stop": stop_after_attempt(3),
        "reraise": True,
    }
