"""Unit tests for TokenBudget and token estimation utilities."""

from __future__ import annotations

import pytest

from agent_core.models import ModelId, ModelProvider
from agent_core.query.budget import (
    TokenBudget,
    estimate_tokens_from_messages,
    get_context_window,
)


class TestGetContextWindow:
    def test_known_anthropic_model(self) -> None:
        mid = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        assert get_context_window(mid) == 200_000

    def test_known_openai_model(self) -> None:
        mid = ModelId(ModelProvider.OPENAI, "gpt-4o")
        assert get_context_window(mid) == 128_000

    def test_large_context_model(self) -> None:
        mid = ModelId(ModelProvider.OPENAI, "gpt-4.1")
        assert get_context_window(mid) == 1_000_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        mid = ModelId(ModelProvider.ANTHROPIC, "some-future-model")
        assert get_context_window(mid) == 200_000


class TestEstimateTokensFromMessages:
    def test_empty(self) -> None:
        assert estimate_tokens_from_messages([]) == 0

    def test_simple_text(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hello, world!"}],
            }
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est == len("Hello, world!") // 4

    def test_with_system(self) -> None:
        msgs: list[dict] = []
        est = estimate_tokens_from_messages(
            msgs, system="You are a helpful assistant."
        )
        assert est == len("You are a helpful assistant.") // 4

    def test_multiple_messages(self) -> None:
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est == (len("Hi") + len("Hello")) // 4

    def test_with_tool_use(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "/f.txt"}},
                ],
            }
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est > 0  # tool_use name + input contribute to count

    def test_with_tool_result(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "file contents here"},
                ],
            }
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est == len("file contents here") // 4


class TestTokenBudget:
    def test_initial_state(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        assert tb.turn_count == 0
        assert tb.total_input_tokens == 0
        assert tb.total_output_tokens == 0
        assert not tb.is_exhausted()
        assert not tb.diminishing_returns()

    def test_truncation_threshold(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        assert tb.truncation_threshold == 160_000  # 80 %

    def test_record_turn(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=1000, output_tokens=200)
        assert tb.turn_count == 1
        assert tb.total_input_tokens == 1000
        assert tb.total_output_tokens == 200

    def test_record_multiple_turns(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=100, output_tokens=50)
        tb.record_turn(2, input_tokens=200, output_tokens=80)
        assert tb.turn_count == 2
        assert tb.total_output_tokens == 130

    def test_is_exhausted(self) -> None:
        tb = TokenBudget(max_turns=2, context_window=200_000)
        assert not tb.is_exhausted()
        tb.record_turn(1, input_tokens=10, output_tokens=5)
        assert not tb.is_exhausted()
        tb.record_turn(2, input_tokens=10, output_tokens=5)
        assert tb.is_exhausted()

    def test_diminishing_returns_not_enough_data(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=100, output_tokens=50)
        tb.record_turn(2, input_tokens=100, output_tokens=60)
        assert not tb.diminishing_returns()  # only 2 data points

    def test_diminishing_returns_triggered(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        # 3 consecutive turns each producing < 500 output tokens
        tb.record_turn(1, input_tokens=1000, output_tokens=100)
        tb.record_turn(2, input_tokens=1000, output_tokens=150)
        tb.record_turn(3, input_tokens=1000, output_tokens=200)
        assert tb.diminishing_returns()

    def test_diminishing_returns_not_triggered(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=1000, output_tokens=600)  # > 500
        tb.record_turn(2, input_tokens=1000, output_tokens=100)
        tb.record_turn(3, input_tokens=1000, output_tokens=200)
        assert not tb.diminishing_returns()

    def test_reset_diminishing_returns(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=1000, output_tokens=100)
        tb.record_turn(2, input_tokens=1000, output_tokens=100)
        tb.record_turn(3, input_tokens=1000, output_tokens=100)
        assert tb.diminishing_returns()
        tb.reset_diminishing_returns()
        assert not tb.diminishing_returns()

    def test_needs_truncation(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        # threshold = 160_000 tokens ≈ 640_000 chars
        assert not tb.needs_truncation(100_000)
        assert tb.needs_truncation(200_000)

    def test_total_tokens(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=100, output_tokens=50)
        tb.record_turn(2, input_tokens=200, output_tokens=80)
        assert tb.total_tokens() == 430

    def test_known_deepseek_model_context_window(self) -> None:
        mid = ModelId(ModelProvider.DEEPSEEK, "deepseek-v4-pro")
        assert get_context_window(mid) == 128_000

    def test_known_openai_gpt41_context_window(self) -> None:
        mid = ModelId(ModelProvider.OPENAI, "gpt-4.1-mini")
        assert get_context_window(mid) == 1_000_000

    def test_diminishing_returns_window_only_keeps_last_three(self) -> None:
        """After 5 turns, only the most recent 3 deltas are tracked."""
        tb = TokenBudget(max_turns=20, context_window=200_000)
        # Turn 1-2: large output
        tb.record_turn(1, input_tokens=100, output_tokens=1000)
        tb.record_turn(2, input_tokens=100, output_tokens=1000)
        # Turn 3-5: small output
        tb.record_turn(3, input_tokens=100, output_tokens=10)
        tb.record_turn(4, input_tokens=100, output_tokens=10)
        tb.record_turn(5, input_tokens=100, output_tokens=10)
        # The window should now contain deltas from turns 3-4-5 (all small)
        assert tb.diminishing_returns()  # all < 500

    def test_diminishing_returns_reset_on_tool_use(self) -> None:
        """After tool use, diminishing returns check resets."""
        tb = TokenBudget(max_turns=20, context_window=200_000)
        tb.record_turn(1, input_tokens=100, output_tokens=10)
        tb.record_turn(2, input_tokens=100, output_tokens=10)
        # Not enough data yet (only 2 turns)
        assert not tb.diminishing_returns()
        tb.reset_diminishing_returns()
        # After reset, window should be empty
        assert not tb.diminishing_returns()

    def test_needs_truncation_at_exact_threshold(self) -> None:
        tb = TokenBudget(max_turns=20, context_window=200_000)
        threshold = tb.truncation_threshold  # 160_000
        # At exactly the threshold
        assert not tb.needs_truncation(threshold)
        # One more
        assert tb.needs_truncation(threshold + 1)

    def test_estimate_tokens_with_mixed_unknown_blocks(self) -> None:
        """Blocks with unknown types should still be counted."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "unknown_type", "data": "some data here"},
                ],
            }
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est > 0  # fallback: str(block) for unknown types

    def test_estimate_tokens_with_nested_content(self) -> None:
        """Nested content structures are recursively counted."""
        msgs = [
            {
                "role": "user",
                "content": "plain string",
            }
        ]
        est = estimate_tokens_from_messages(msgs)
        assert est == len("plain string") // 4

    def test_estimate_tokens_with_empty_content(self) -> None:
        msgs = [{"role": "user", "content": []}]
        est = estimate_tokens_from_messages(msgs)
        assert est == 0
