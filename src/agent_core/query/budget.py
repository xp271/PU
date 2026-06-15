"""
Token budget management for the agent query loop.

Tracks per-turn token consumption, detects diminishing returns, and
provides context-window-aware truncation thresholds.

Token estimation uses a simple heuristic (chars / 4) to avoid pulling
in a tokenizer dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import ModelId, ModelProvider

# ---------------------------------------------------------------------------
# Context window sizes (model-aware)
# ---------------------------------------------------------------------------

_MODEL_CONTEXT_WINDOWS: dict[tuple[ModelProvider, str | None], int] = {
    # Anthropic
    (ModelProvider.ANTHROPIC, "claude-sonnet-4-5"): 200_000,
    (ModelProvider.ANTHROPIC, "claude-opus-4-7"): 200_000,
    (ModelProvider.ANTHROPIC, "claude-haiku-4-5"): 200_000,
    # OpenAI
    (ModelProvider.OPENAI, "gpt-4o"): 128_000,
    (ModelProvider.OPENAI, "gpt-4.1"): 1_000_000,
    (ModelProvider.OPENAI, "gpt-4.1-mini"): 1_000_000,
    (ModelProvider.OPENAI, "o4-mini"): 200_000,
    # DeepSeek
    (ModelProvider.DEEPSEEK, "deepseek-v4-pro"): 128_000,
    (ModelProvider.DEEPSEEK, "deepseek-v4-flash"): 128_000,
}

_DEFAULT_CONTEXT_WINDOW = 200_000
_TRUNCATION_RATIO = 0.8
_DIMINISHING_RETURNS_THRESHOLD = 500
_DIMINISHING_RETURNS_WINDOW = 3


def get_context_window(model_id: ModelId) -> int:
    """Return the context-window size for *model_id*.

    Falls back to 200 000 tokens for unknown models.
    """
    return _MODEL_CONTEXT_WINDOWS.get(
        (model_id.provider, model_id.name), _DEFAULT_CONTEXT_WINDOW
    )


# ---------------------------------------------------------------------------
# Token estimation (heuristic — chars / 4)
# ---------------------------------------------------------------------------


def _content_chars(content: Any) -> int:
    """Recursively sum character counts for any content structure."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    total += len(str(block.get("text", "")))
                elif block_type == "tool_use":
                    total += len(str(block.get("name", "")))
                    total += len(str(block.get("input", {})))
                elif block_type == "tool_result":
                    total += len(str(block.get("content", "")))
                # fallback: stringify the whole block
                else:
                    total += len(str(block))
            elif isinstance(block, str):
                total += len(block)
        return total
    return len(str(content))


def estimate_tokens_from_messages(
    messages: list[dict[str, Any]], system: str | None = None
) -> int:
    """Rough token count: total character count / 4."""
    total_chars = 0

    if system:
        total_chars += len(system)

    for msg in messages:
        total_chars += _content_chars(msg.get("content", []))

    return total_chars // 4


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------


@dataclass
class TokenBudget:
    """Tracks token consumption across turns and detects stop conditions."""

    max_turns: int
    context_window: int

    # -- runtime state --
    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # -- diminishing-returns tracking --
    _last_three_deltas: list[int] = field(default_factory=list)
    _previous_output_total: int = 0

    @property
    def truncation_threshold(self) -> int:
        return int(self.context_window * _TRUNCATION_RATIO)

    def record_turn(
        self, turn_index: int, input_tokens: int, output_tokens: int
    ) -> None:
        """Record token usage for the turn that just finished."""
        self.turn_count += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

        current = self.total_output_tokens
        delta = current - self._previous_output_total
        self._previous_output_total = current

        self._last_three_deltas.append(delta)
        if len(self._last_three_deltas) > _DIMINISHING_RETURNS_WINDOW:
            self._last_three_deltas.pop(0)

    def is_exhausted(self) -> bool:
        """True when the turn budget is spent."""
        return self.turn_count >= self.max_turns

    def diminishing_returns(self) -> bool:
        """True when each of the last 3 turns produced < 500 output tokens.

        This is a heuristic stop condition: the model has likely finished
        meaningful work and is just spinning.
        """
        if len(self._last_three_deltas) < _DIMINISHING_RETURNS_WINDOW:
            return False
        return all(
            d < _DIMINISHING_RETURNS_THRESHOLD
            for d in self._last_three_deltas
        )

    def reset_diminishing_returns(self) -> None:
        """Reset the window — call after a turn that included tool use."""
        self._last_three_deltas.clear()

    def needs_truncation(self, estimated_tokens: int) -> bool:
        """True when the message history should be hard-truncated."""
        return estimated_tokens > self.truncation_threshold

    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens
