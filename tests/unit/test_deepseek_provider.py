"""Tests for the DeepSeek LLM provider."""

from __future__ import annotations

import httpx
import openai
import pytest

from agent_core.llm.deepseek import DeepSeekClient
from agent_core.models import ModelProvider


def _make_status_error(status_code: int) -> openai.APIStatusError:
    """Construct a minimal APIStatusError with the given status code."""
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
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
    def test_429_is_rate_limit(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(429))
            == "rate_limit"
        )

    def test_503_is_overloaded(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(503))
            == "overloaded"
        )

    def test_500_is_transient(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(500))
            == "transient"
        )

    def test_502_is_transient(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(502))
            == "transient"
        )

    def test_402_is_fatal(self) -> None:
        """DeepSeek-specific: 402 balance exhausted is fatal."""
        assert (
            DeepSeekClient.classify_error(_make_status_error(402))
            == "fatal"
        )

    def test_401_is_fatal(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(401))
            == "fatal"
        )

    def test_403_is_fatal(self) -> None:
        assert (
            DeepSeekClient.classify_error(_make_status_error(403))
            == "fatal"
        )

    def test_non_api_error_is_transient(self) -> None:
        assert (
            DeepSeekClient.classify_error(ValueError("something"))
            == "transient"
        )


# ---------------------------------------------------------------------------
# normalize_stop_reason
# ---------------------------------------------------------------------------

@pytest.mark.model
class TestNormalizeStopReason:
    def test_stop_maps_to_end_turn(self) -> None:
        mapped, detail = DeepSeekClient.normalize_stop_reason("stop")
        assert mapped == "end_turn"
        assert detail is None

    def test_length_maps_to_max_turns(self) -> None:
        mapped, detail = DeepSeekClient.normalize_stop_reason("length")
        assert mapped == "max_turns"
        assert detail is None

    def test_tool_calls_maps_to_end_turn(self) -> None:
        mapped, detail = DeepSeekClient.normalize_stop_reason("tool_calls")
        assert mapped == "end_turn"
        assert detail is None

    def test_content_filter_maps_to_permission_denied(self) -> None:
        mapped, detail = DeepSeekClient.normalize_stop_reason("content_filter")
        assert mapped == "permission_denied"
        assert detail == "deepseek finish_reason=content_filter"

    def test_unknown_stop_reason_falls_back_to_end_turn(self) -> None:
        mapped, detail = DeepSeekClient.normalize_stop_reason("unknown_reason")
        assert mapped == "end_turn"
        assert detail is None


# ---------------------------------------------------------------------------
# convert_messages
# ---------------------------------------------------------------------------

@pytest.mark.message
class TestConvertMessages:
    def test_system_only(self) -> None:
        result = DeepSeekClient.convert_messages([], "You are helpful.")
        assert result == [{"role": "system", "content": "You are helpful."}]

    def test_simple_user_message(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]
        result = DeepSeekClient.convert_messages(messages, None)
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
        result = DeepSeekClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tu_1"

    def test_assistant_with_text(self) -> None:
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "I'll help."}]}
        ]
        result = DeepSeekClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "I'll help."

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
        result = DeepSeekClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["tool_calls"][0]["id"] == "call_1"
        assert result[0]["tool_calls"][0]["function"]["name"] == "Read"

    def test_string_content_is_passed_through(self) -> None:
        messages = [{"role": "user", "content": "plain text"}]
        result = DeepSeekClient.convert_messages(messages, None)
        assert result == [{"role": "user", "content": "plain text"}]


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

@pytest.mark.model
class TestInit:
    def test_default_base_url_is_deepseek(self) -> None:
        client = DeepSeekClient(model="deepseek-v4-flash", api_key="sk-test", base_url=None)
        assert client._client.base_url.host == "api.deepseek.com"

    def test_custom_base_url_overrides_default(self) -> None:
        client = DeepSeekClient(
            model="deepseek-v4-flash",
            api_key="sk-test",
            base_url="https://custom-proxy.example.com",
        )
        assert client._client.base_url.host == "custom-proxy.example.com"

    def test_provider_is_deepseek(self) -> None:
        assert DeepSeekClient.provider == ModelProvider.DEEPSEEK
