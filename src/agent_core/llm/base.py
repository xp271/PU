"""
Provider-agnostic LLM client abstraction.

Each provider implementation extends ``BaseClient`` and translates its SDK's
streaming response into a uniform ``InternalEvent`` stream.  The agent's
``query()`` loop never knows which provider is in use — it only consumes
``InternalEvent``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from .events import InternalEvent

if TYPE_CHECKING:
    from ..models import AgentConfig, ModelId


class BaseClient(ABC):
    """Abstract base for a provider-specific streaming LLM client.

    Each provider implementation:
    - Accepts internal-format messages, tools, and a system prompt.
    - Yields a uniform stream of ``InternalEvent`` items.
    - Handles its own SDK-level error recovery (retries, back-off).

    Lifecycle: construct -> ``stream_messages()`` -> discard (stateless,
    each call is independent).
    """

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        """Send messages and yield ``InternalEvent`` items as they arrive.

        Parameters
        ----------
        messages:
            Internal-format message history (Anthropic content-block structure).
        tools:
            Tool schemas in provider-native format (from ``tool_schema.py``).
        system:
            System prompt (``None`` = no system prompt).
        max_tokens:
            Maximum output tokens for this request.
        turn_index:
            Current turn number (for ``StreamStart`` metadata).

        Yields
        ------
        StreamStart -> AssistantText* -> (ToolUse)* -> TokenCounts -> StopEvent
        On unrecoverable error: ErrorMessage -> StopEvent(stop_reason="error")
        """
        ...


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def get_client(model_id: "ModelId", config: "AgentConfig") -> BaseClient:
    """Route to the correct ``BaseClient`` implementation by provider.

    Imports are deferred so that an uninstalled SDK only raises ``ImportError``
    when its provider is actually requested — not at module load time.
    """
    from ..models import ModelProvider

    provider = model_id.provider
    api_key = config.get_api_key(provider)
    base_url = config.get_base_url(provider)

    match provider:
        case ModelProvider.ANTHROPIC:
            from .anthropic import AnthropicClient

            return AnthropicClient(
                model=model_id.name,
                api_key=api_key,
                base_url=base_url,
            )
        case ModelProvider.OPENAI:
            from .openai import OpenAIClient

            return OpenAIClient(
                model=model_id.name,
                api_key=api_key,
                base_url=base_url,
            )
        case _:
            raise ValueError(
                f"No client implementation for provider: {provider}"
            )
