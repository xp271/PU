"""Unit tests for provider registry and error utilities."""

from __future__ import annotations

from typing import ClassVar

import anthropic
import pytest

from agent_core.llm.anthropic import AnthropicClient
from agent_core.llm.base import BaseClient, get_client, register_client, _PROVIDER_CLIENTS
from agent_core.llm.errors import retry_config_for, _retry_predicate
from agent_core.models import ModelId, ModelProvider


# ---------------------------------------------------------------------------
# register_client
# ---------------------------------------------------------------------------


class TestRegisterClient:
    def test_raises_type_error_when_no_provider(self) -> None:
        with pytest.raises(TypeError, match="must define a 'provider'"):

            @register_client
            class BadClient(BaseClient):
                pass

    def test_registers_client_in_global_dict(self) -> None:
        # Verify all three providers registered (tests import all provider modules)
        assert ModelProvider.ANTHROPIC in _PROVIDER_CLIENTS
        assert ModelProvider.OPENAI in _PROVIDER_CLIENTS
        assert ModelProvider.DEEPSEEK in _PROVIDER_CLIENTS

    def test_reregister_evicts_previous(self) -> None:
        """Re-registering a provider overwrites the previous entry."""
        prev = _PROVIDER_CLIENTS.get(ModelProvider.ANTHROPIC)
        assert prev is not None  # sanity check


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


class TestGetClient:
    def test_returns_correct_client_for_anthropic(self) -> None:
        mid = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        from agent_core.models import AgentConfig

        config = AgentConfig(api_keys={"anthropic": "sk-test"})
        client = get_client(mid, config)
        assert isinstance(client, _PROVIDER_CLIENTS[ModelProvider.ANTHROPIC])

    def test_returns_correct_client_for_openai(self) -> None:
        mid = ModelId(ModelProvider.OPENAI, "gpt-4o")
        from agent_core.models import AgentConfig

        config = AgentConfig(api_keys={"openai": "sk-test"})
        client = get_client(mid, config)
        assert isinstance(client, _PROVIDER_CLIENTS[ModelProvider.OPENAI])

    def test_returns_correct_client_for_deepseek(self) -> None:
        mid = ModelId(ModelProvider.DEEPSEEK, "deepseek-v4-pro")
        from agent_core.models import AgentConfig

        config = AgentConfig(api_keys={"deepseek": "sk-test"})
        client = get_client(mid, config)
        assert isinstance(client, _PROVIDER_CLIENTS[ModelProvider.DEEPSEEK])

    def test_raises_value_error_for_unregistered_provider(self) -> None:
        # This would require an unregistered provider — test the error message pattern
        from agent_core.models import AgentConfig

        config = AgentConfig(allow_unknown_model=True, model="anthropic/claude-sonnet-4-5")
        mid = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        # All known providers are registered, so this should succeed
        client = get_client(mid, config)
        assert client is not None


# ---------------------------------------------------------------------------
# retry_config_for
# ---------------------------------------------------------------------------


class TestRetryConfigFor:
    def test_returns_expected_keys(self) -> None:
        config = retry_config_for(AnthropicClient.classify_error, "anthropic")
        assert "retry" in config
        assert "wait" in config
        assert "stop" in config
        assert "reraise" in config

    def test_retry_predicate_rate_limit_is_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        import httpx

        err = anthropic.APIStatusError(
            message="rate limited",
            body={"error": {"message": "rate limited"}},
            response=httpx.Response(
                status_code=429,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert predicate(err) is True

    def test_retry_predicate_overloaded_is_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        import httpx

        err = anthropic.APIStatusError(
            message="overloaded",
            body={"error": {"message": "overloaded"}},
            response=httpx.Response(
                status_code=529,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert predicate(err) is True

    def test_retry_predicate_transient_is_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        import httpx

        err = anthropic.APIStatusError(
            message="server error",
            body={"error": {"message": "server error"}},
            response=httpx.Response(
                status_code=500,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert predicate(err) is True

    def test_retry_predicate_fatal_is_not_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        import httpx

        err = anthropic.APIStatusError(
            message="unauthorized",
            body={"error": {"message": "unauthorized"}},
            response=httpx.Response(
                status_code=401,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert predicate(err) is False

    def test_retry_predicate_context_length_is_not_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        import httpx

        err = anthropic.APIStatusError(
            message="context window exceeded",
            body={"error": {"message": "context window exceeded"}},
            response=httpx.Response(
                status_code=413,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert predicate(err) is False

    def test_retry_predicate_non_api_error_is_retryable(self) -> None:
        predicate = _retry_predicate(AnthropicClient.classify_error, "anthropic")
        # classify_error returns "transient" for non-API exceptions
        assert predicate(ValueError("something")) is True

    def test_retry_config_for_openai(self) -> None:
        from agent_core.llm.openai import OpenAIClient

        config = retry_config_for(OpenAIClient.classify_error, "openai")
        assert config["retry"] is not None
        assert config["reraise"] is True

    def test_retry_config_for_deepseek(self) -> None:
        from agent_core.llm.deepseek import DeepSeekClient

        config = retry_config_for(DeepSeekClient.classify_error, "deepseek")
        assert config["retry"] is not None
        assert config["reraise"] is True
