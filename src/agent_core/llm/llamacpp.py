"""
llama.cpp local-model streaming client.

Talks to a locally running ``llama-server.exe`` via its OpenAI-compatible
``/v1/chat/completions`` endpoint.  Inherits ``OpenAIClient`` wholesale —
only overrides constructor defaults and error classification.

Key differences from OpenAI
---------------------------
- Default ``base_url`` is ``http://127.0.0.1:8080/v1``.
- No API key required (llama-server has no auth by default).
- ``APIConnectionError`` is always retryable (server may not be running).
- ``StreamStart.provider`` is ``"llamacpp"`` (inherited via ``self.provider.value``).
"""

from __future__ import annotations

from typing import ClassVar

import openai

from ..models import ModelProvider
from .base import BaseClient, register_client
from .errors import ErrorCategory
from .openai import OpenAIClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@register_client
class LlamaCppClient(OpenAIClient):
    """Streaming client backed by a local llama.cpp server (OpenAI-compatible API)."""

    provider: ClassVar[ModelProvider] = ModelProvider.LLAMACPP

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None,
        sampling_kwargs: dict[str, Any] | None = None,
        sampling_extra: dict[str, Any] | None = None,
    ) -> None:
        import httpx

        super().__init__(
            model=model,
            api_key=api_key or "not-needed",
            base_url=base_url or _DEFAULT_BASE_URL,
            sampling_kwargs=sampling_kwargs,
            sampling_extra=sampling_extra,
        )
        # Re-create the OpenAI client with system proxy bypassed —
        # local server must be reached directly on 127.0.0.1.
        kwargs: dict[str, Any] = {
            "api_key": api_key or "not-needed",
            "base_url": base_url or _DEFAULT_BASE_URL,
            "http_client": httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(proxy=None),
            ),
        }
        self._client = openai.AsyncOpenAI(**kwargs)

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Connection refused / timeout → retryable; delegate the rest to OpenAI logic."""
        if isinstance(exception, openai.APIConnectionError):
            return "transient"
        return super().classify_error(exception)
