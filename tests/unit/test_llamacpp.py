"""Tests for the llama.cpp local-model LLM provider."""

from __future__ import annotations

import httpx
import openai
import pytest

from agent_core.llm.llamacpp import LlamaCppClient
from agent_core.models import ModelProvider


def _make_status_error(status_code: int) -> openai.APIStatusError:
    """Construct a minimal APIStatusError with the given status code."""
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    response = httpx.Response(status_code=status_code, request=request)
    return openai.APIStatusError(
        message=f"Error {status_code}",
        body={"error": {"message": f"Error {status_code}"}},
        response=response,
    )


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestClassifyError:
    def test_connection_error_is_transient(self) -> None:
        """APIConnectionError (server not running) should be retryable."""
        err = openai.APIConnectionError(request=None, message="Connection refused")
        assert LlamaCppClient.classify_error(err) == "transient"

    def test_connection_timeout_is_transient(self) -> None:
        """APIConnectionError with timeout should also be retryable."""
        err = openai.APIConnectionError(request=None, message="Connection timed out")
        assert LlamaCppClient.classify_error(err) == "transient"

    def test_429_is_rate_limit(self) -> None:
        assert LlamaCppClient.classify_error(_make_status_error(429)) == "rate_limit"

    def test_401_is_fatal(self) -> None:
        assert LlamaCppClient.classify_error(_make_status_error(401)) == "fatal"

    def test_500_is_transient(self) -> None:
        assert LlamaCppClient.classify_error(_make_status_error(500)) == "transient"

    def test_413_is_context_length(self) -> None:
        assert LlamaCppClient.classify_error(_make_status_error(413)) == "context_length"

    def test_non_api_error_is_transient(self) -> None:
        assert LlamaCppClient.classify_error(ValueError("something")) == "transient"


# ---------------------------------------------------------------------------
# normalize_stop_reason
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestNormalizeStopReason:
    def test_stop_maps_to_end_turn(self) -> None:
        mapped, detail = LlamaCppClient.normalize_stop_reason("stop")
        assert mapped == "end_turn"
        assert detail is None

    def test_length_maps_to_max_turns(self) -> None:
        mapped, detail = LlamaCppClient.normalize_stop_reason("length")
        assert mapped == "max_turns"
        assert detail is None

    def test_tool_calls_maps_to_end_turn(self) -> None:
        mapped, detail = LlamaCppClient.normalize_stop_reason("tool_calls")
        assert mapped == "end_turn"
        assert detail is None

    def test_content_filter_maps_to_permission_denied(self) -> None:
        mapped, detail = LlamaCppClient.normalize_stop_reason("content_filter")
        assert mapped == "permission_denied"


# ---------------------------------------------------------------------------
# convert_messages (inherited from OpenAIClient)
# ---------------------------------------------------------------------------


@pytest.mark.message
class TestConvertMessages:
    def test_system_only(self) -> None:
        result = LlamaCppClient.convert_messages([], "You are helpful.")
        assert result == [{"role": "system", "content": "You are helpful."}]

    def test_simple_user_message(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]
        result = LlamaCppClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"

    def test_user_with_tool_result(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "result text"},
                ],
            }
        ]
        result = LlamaCppClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tu_1"

    def test_assistant_with_tool_use(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x.txt"},
                    }
                ],
            }
        ]
        result = LlamaCppClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["tool_calls"][0]["id"] == "call_1"
        assert result[0]["tool_calls"][0]["function"]["name"] == "Read"


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestInit:
    def test_default_base_url_is_localhost(self) -> None:
        client = LlamaCppClient(model="test-model", api_key=None, base_url=None)
        assert str(client._client.base_url).rstrip("/") == "http://127.0.0.1:8080/v1"

    def test_custom_base_url_overrides_default(self) -> None:
        client = LlamaCppClient(
            model="test-model",
            api_key=None,
            base_url="http://192.168.1.100:9090/v1",
        )
        assert str(client._client.base_url).rstrip("/") == "http://192.168.1.100:9090/v1"

    def test_api_key_defaults_to_not_needed(self) -> None:
        client = LlamaCppClient(model="test-model", api_key=None, base_url=None)
        assert client.api_key == "not-needed"

    def test_api_key_passed_through(self) -> None:
        client = LlamaCppClient(model="test-model", api_key="custom-key", base_url=None)
        assert client.api_key == "custom-key"

    def test_provider_is_llamacpp(self) -> None:
        assert LlamaCppClient.provider == ModelProvider.LLAMACPP


# ---------------------------------------------------------------------------
# StreamStart provider (inherited via self.provider.value)
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestStreamStartProvider:
    def test_provider_value_string_is_llamacpp(self) -> None:
        """Verify the provider value used in StreamStart is 'llamacpp'."""
        client = LlamaCppClient(model="test-model", api_key=None, base_url=None)
        assert client.provider.value == "llamacpp"
