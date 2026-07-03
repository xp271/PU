"""
Provider-agnostic LLM client abstraction.

Each provider implementation extends ``BaseClient`` and translates its SDK's
streaming response into a uniform ``InternalEvent`` stream.  The agent's
``query()`` loop never knows which provider is in use — it only consumes
``InternalEvent``.

Adding a new provider requires only:
1. Subclass ``BaseClient`` with a ``provider`` ClassVar and the ``@register_client`` decorator.
2. Implement the abstract methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import ModelProvider
from .errors import ErrorCategory
from .events import InternalEvent

if TYPE_CHECKING:
    from ..models import AgentConfig, ModelId


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_CLIENTS: dict[ModelProvider, type[BaseClient]] = {}


def register_client(cls: type[BaseClient]) -> type[BaseClient]:
    """Decorator that registers a ``BaseClient`` subclass for its provider.

    Usage::

        @register_client
        class AnthropicClient(BaseClient):
            provider: ClassVar[ModelProvider] = ModelProvider.ANTHROPIC
            ...
    """
    provider = getattr(cls, "provider", None)
    if not isinstance(provider, ModelProvider):
        raise TypeError(
            f"{cls.__name__} must define a 'provider' ClassVar[ModelProvider]"
        )
    _PROVIDER_CLIENTS[provider] = cls
    return cls


def get_client(model_id: "ModelId", config: "AgentConfig") -> BaseClient:
    """Route to the correct ``BaseClient`` implementation by provider.

    Delegates to the registry populated by ``register_client()`` calls in
    each provider module.
    """
    provider = model_id.provider
    client_cls = _PROVIDER_CLIENTS.get(provider)
    if client_cls is None:
        raise ValueError(
            f"No client registered for provider: {provider}. "
            f"Available providers: {list(_PROVIDER_CLIENTS)}"
        )
    sampling_kwargs: dict[str, Any] = {}
    sampling_extra: dict[str, Any] = {}
    if config.sampling and provider == ModelProvider.LLAMACPP:
        sampling_kwargs, sampling_extra = _sampling_params(config.sampling)
    return client_cls(
        model=model_id.name,
        api_key=config.get_api_key(provider),
        base_url=config.get_base_url(provider),
        sampling_kwargs=sampling_kwargs if sampling_kwargs else None,
        sampling_extra=sampling_extra if sampling_extra else None,
    )


def _sampling_params(sampling: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split sampling config into (openai_kwargs, extra_body).

    Standard OpenAI params (temperature, top_p) go to kwargs.
    llama.cpp extensions (top_k, repeat_penalty) go to extra_body,
    which the OpenAI SDK forwards as-is in the JSON body.
    """
    kwargs: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    if sampling.temperature is not None:
        kwargs["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        kwargs["top_p"] = sampling.top_p
    if sampling.top_k is not None:
        extra["top_k"] = sampling.top_k
    if sampling.repeat_penalty is not None:
        extra["repeat_penalty"] = sampling.repeat_penalty
    return kwargs, extra


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseClient(ABC):
    """Abstract base for a provider-specific streaming LLM client.

    Each subclass bundles everything provider-specific in one place:
    - outbound message conversion
    - tool schema conversion
    - stop-reason mapping
    - error classification
    - the streaming call itself

    Lifecycle: construct → ``stream_messages()`` → discard (stateless,
    each call is independent).
    """

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None,
        sampling_kwargs: dict[str, Any] | None = None,
        sampling_extra: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.sampling_kwargs = sampling_kwargs
        self.sampling_extra = sampling_extra

    # -- provider-specific class methods (override in each subclass) --

    @classmethod
    def convert_messages(
        cls,
        messages: list[dict[str, Any]],
        system: str | None,
    ) -> Any:
        """Convert internal messages to provider-native API format.

        Returns a provider-specific structure (e.g. a tuple or list) ready
        to be passed to the SDK.
        """
        raise NotImplementedError

    @classmethod
    def convert_tools(
        cls,
        allowed_tools: list[str],
        registry: Any = None,
    ) -> list[dict[str, Any]]:
        """Build a ``tools`` parameter list in provider-native format.

        *registry* is a ``ToolRegistry`` instance.  When ``None`` the
        provider falls back to the module-level default (backward compat).
        """
        raise NotImplementedError

    @classmethod
    def normalize_stop_reason(
        cls, raw_reason: str
    ) -> tuple[str, str | None]:
        """Map a provider-raw stop reason to (internal_reason, error_detail)."""
        raise NotImplementedError

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Classify a provider SDK exception into an ``ErrorCategory``."""
        raise NotImplementedError

    # -- streaming (instance method) --

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
            Tool schemas in provider-native format.
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
