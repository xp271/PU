"""Retry-loop fallback tests for the LLM providers (Item 3).

``classify_error`` and the retry *predicate* are tested elsewhere; this file
covers the actual retry *orchestration* inside each provider's
``stream_messages``:

* a retryable error → exponential-backoff retry → eventual success
* retries exhausted → ``ErrorMessage`` + ``StopEvent(stop_reason="error")``
* a fatal error → immediate error, no retry / no sleep

The single-attempt ``_do_stream`` helper is replaced per-test so no real SDK
or network is involved; ``asyncio.sleep`` is patched so backoff is instant.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_core.llm.anthropic import AnthropicClient
from agent_core.llm.deepseek import DeepSeekClient
from agent_core.llm.events import AssistantText, ErrorMessage, StopEvent
from agent_core.llm.openai import OpenAIClient


# ---------------------------------------------------------------------------
# Provider-specific status-error factories
# ---------------------------------------------------------------------------


def _anthropic_status_error(status: int):
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status_code=status, request=req)
    return anthropic.APIStatusError(
        message=f"Error {status}",
        body={"error": {"message": f"Error {status}"}},
        response=resp,
    )


def _openai_status_error(status: int):
    import openai

    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(status_code=status, request=req)
    return openai.APIStatusError(message=f"Error {status}", response=resp, body=None)


# (label, client factory, status-error factory)
PROVIDERS = [
    pytest.param(
        lambda: AnthropicClient(model="claude-sonnet-4-5", api_key="sk-test", base_url=None),
        _anthropic_status_error,
        id="anthropic",
    ),
    pytest.param(
        lambda: OpenAIClient(model="gpt-4o", api_key="sk-test", base_url=None),
        _openai_status_error,
        id="openai",
    ),
    pytest.param(
        lambda: DeepSeekClient(model="deepseek-chat", api_key="sk-test", base_url=None),
        _openai_status_error,
        id="deepseek",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain(client) -> list:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    async def _run():
        out = []
        async for ev in client.stream_messages(messages=msgs, tools=[], turn_index=0):
            out.append(ev)
        return out

    return asyncio.run(_run())


def _install_fake_stream(client, behavior: str, status_error_factory):
    """Replace ``client._do_stream`` with a controllable fake; return a dict
    tracking the number of attempts."""
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if behavior == "retry_then_succeed":
            if calls["n"] == 1:
                raise ValueError("transient network blip")  # → "transient"
            yield AssistantText(content="recovered")
            yield StopEvent(stop_reason="end_turn")
        elif behavior == "always_transient":
            raise ValueError("transient network blip")
            yield  # pragma: no cover — makes this an async generator
        elif behavior == "fatal":
            raise status_error_factory(401)  # → "fatal"
            yield  # pragma: no cover
        else:  # pragma: no cover
            raise AssertionError(behavior)

    client._do_stream = fake
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_factory,status_error_factory", PROVIDERS)
class TestStreamRetryLoop:
    def test_retryable_error_then_success(self, client_factory, status_error_factory):
        client = client_factory()
        calls = _install_fake_stream(client, "retry_then_succeed", status_error_factory)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
            events = _drain(client)

        assert calls["n"] == 2  # failed once, retried, then succeeded
        assert any(isinstance(e, AssistantText) for e in events)
        assert not any(isinstance(e, ErrorMessage) for e in events)
        sleep_mock.assert_awaited()  # backoff happened between attempts

    def test_retries_exhausted_yields_error(self, client_factory, status_error_factory):
        client = client_factory()
        calls = _install_fake_stream(client, "always_transient", status_error_factory)
        with patch("asyncio.sleep", new=AsyncMock()):
            events = _drain(client)

        # range(max_retries + 1) = 4 attempts (3 retries + the final)
        assert calls["n"] == 4
        errors = [e for e in events if isinstance(e, ErrorMessage)]
        assert len(errors) == 1
        stops = [e for e in events if isinstance(e, StopEvent)]
        assert stops and stops[-1].stop_reason == "error"

    def test_fatal_error_is_not_retried(self, client_factory, status_error_factory):
        client = client_factory()
        calls = _install_fake_stream(client, "fatal", status_error_factory)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
            events = _drain(client)

        assert calls["n"] == 1  # no retry on a fatal error
        sleep_mock.assert_not_awaited()
        errors = [e for e in events if isinstance(e, ErrorMessage)]
        assert len(errors) == 1
        assert errors[0].error_type == "fatal"
        stops = [e for e in events if isinstance(e, StopEvent)]
        assert stops and stops[-1].stop_reason == "error"
