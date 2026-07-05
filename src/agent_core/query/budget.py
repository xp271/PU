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

# Provider-level defaults when the specific model name isn't in the map.
_PROVIDER_CONTEXT_DEFAULTS: dict[ModelProvider, int] = {
    ModelProvider.LLAMACPP: 32768,
}

_DEFAULT_CONTEXT_WINDOW = 200_000
_TRUNCATION_RATIO = 0.85
_DIMINISHING_RETURNS_THRESHOLD = 500
_DIMINISHING_RETURNS_WINDOW = 3


def get_context_window(model_id: ModelId, *, config_override: int | None = None) -> int:
    """Return the context-window size for *model_id*.

    Resolution order:
    1. *config_override* (e.g. ``AgentConfig.context_size`` from config file).
    2. Exact model match in ``_MODEL_CONTEXT_WINDOWS``.
    3. Provider-level default in ``_PROVIDER_CONTEXT_DEFAULTS``.
    4. Global fallback (200 000).
    """
    if config_override is not None:
        return config_override
    exact = _MODEL_CONTEXT_WINDOWS.get((model_id.provider, model_id.name))
    if exact is not None:
        return exact
    provider_default = _PROVIDER_CONTEXT_DEFAULTS.get(model_id.provider)
    if provider_default is not None:
        return provider_default
    return _DEFAULT_CONTEXT_WINDOW


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

    # 上一轮 API 返回的实际 input_tokens（ground truth），
    # 用于校准 needs_truncation 的启发式估算偏差。
    last_actual_input_tokens: int = 0

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

        self.last_actual_input_tokens = input_tokens

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
        """True when the message history should be hard-truncated.

        优先使用上一轮 API 返回的实际 input_tokens，因为它精确反映
        真实使用量；启发式估算作为兜底（第一轮或实际值不可用时）。
        """
        effective = max(estimated_tokens, self.last_actual_input_tokens)
        return effective > self.truncation_threshold

    def reset_last_actual(self) -> None:
        """压缩/截断后重置实际 token 计数。

        消息列表已被裁剪后，旧的 last_actual_input_tokens 不再有效，
        必须重置以避免下一轮误判仍需截断。
        """
        self.last_actual_input_tokens = 0

    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens
