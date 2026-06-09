"""
Bidirectional message-format conversion between the internal Anthropic
content-block representation and provider-native API formats.

Internal storage format
-----------------------
All messages are stored as Anthropic-style content-block lists:

.. code-block:: python

    # User message with tool_result blocks
    {"role": "user", "content": [
        {"type": "text", "text": "Read the file"},
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "..."},
    ]}

    # Assistant message with tool_use blocks
    {"role": "assistant", "content": [
        {"type": "text", "text": "Let me read that file."},
        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {...}},
    ]}
"""

from __future__ import annotations

import json
from typing import Any

from ..models import ModelProvider


# ---------------------------------------------------------------------------
# Outbound: internal format -> provider API format
# ---------------------------------------------------------------------------

def messages_for_anthropic(
    messages: list[dict[str, Any]],
    system: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert internal messages to Anthropic Messages API parameters.

    Anthropic's content-block format is our internal format, so this is a
    near-identity transform.  We only need to:
    1. Strip internal metadata fields (``_timestamp``, ``_turn``).
    2. Return ``system`` as a separate parameter (not as messages[0]).
    """
    clean = [_clean_message(m) for m in messages]
    return system, clean


def messages_for_openai(
    messages: list[dict[str, Any]],
    system: str | None,
) -> list[dict[str, Any]]:
    """Convert internal Anthropic content-block messages to OpenAI format.

    Conversion rules
    ----------------
    1. System prompt -> first message ``{"role": "system", "content": system}``
    2. User message with tool_result blocks -> split into:
       - One user message (text-only parts)
       - One ``role: "tool"`` message per tool_result block
    3. Assistant message with tool_use blocks -> ``role: "assistant"`` with
       ``tool_calls`` array and ``content`` (or ``null`` when tool-only).
    4. Empty assistant content -> ``content=None`` (OpenAI requirement).
    """
    openai_messages: list[dict[str, Any]] = []

    # 1. System prompt
    if system:
        openai_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", [])

        if isinstance(content, str):
            # Plain text message — pass through unchanged
            openai_messages.append({"role": role, "content": content})
            continue

        if role == "user":
            text_parts: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in content:
                if block.get("type") == "tool_result":
                    tool_results.append(block)
                else:
                    text_parts.append(block)

            # Text portion -> single user message
            if text_parts:
                openai_messages.append({
                    "role": "user",
                    "content": _blocks_to_openai_text(text_parts),
                })

            # tool_result blocks -> individual tool messages
            for tr in tool_results:
                result_content = tr.get("content", "")
                if isinstance(result_content, (list, dict)):
                    result_content = json.dumps(result_content, ensure_ascii=False)
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": str(result_content),
                })

        elif role == "assistant":
            text_content = ""
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if block.get("type") == "text":
                    text_content += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False),
                        },
                    })

            msg_out: dict[str, Any] = {"role": "assistant"}
            if text_content:
                msg_out["content"] = text_content
            else:
                msg_out["content"] = None  # OpenAI: tool-only msg -> content=null
            if tool_calls:
                msg_out["tool_calls"] = tool_calls
            openai_messages.append(msg_out)

        # system / other roles are pass-through (shouldn't appear in practice)
        else:
            openai_messages.append({"role": role, "content": content})

    return openai_messages


# ---------------------------------------------------------------------------
# Stop-reason normalization
# ---------------------------------------------------------------------------

ANTHROPIC_STOP_MAP: dict[str, str] = {
    "end_turn": "end_turn",
    "max_tokens": "max_turns",
    "stop_sequence": "end_turn",
    "tool_use": "end_turn",       # query() continues loop, not terminal
}

OPENAI_STOP_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_turns",
    "tool_calls": "end_turn",     # query() continues loop, not terminal
    "content_filter": "permission_denied",
}


def normalize_stop_reason(
    provider: ModelProvider, raw_reason: str
) -> tuple[str, str | None]:
    """Map a provider-raw stop reason to an internal StopReason.

    Returns
    -------
    (stop_reason, error_detail)
        - ``"end_turn"`` / ``"tool_use"`` -> query() checks for pending tool_use blocks
        - ``"max_turns"`` -> query() checks max_output_tokens recovery
        - ``"permission_denied"`` / ``"error"`` -> terminate immediately
    """
    match provider:
        case ModelProvider.ANTHROPIC:
            mapped = ANTHROPIC_STOP_MAP.get(raw_reason, "end_turn")
            return mapped, None
        case ModelProvider.OPENAI:
            mapped = OPENAI_STOP_MAP.get(raw_reason, "end_turn")
            detail = (
                f"openai finish_reason={raw_reason}"
                if raw_reason == "content_filter"
                else None
            )
            return mapped, detail
    return "end_turn", None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Strip internal metadata keys from a single message dict."""
    return {
        k: v
        for k, v in msg.items()
        if not k.startswith("_")  # filter _timestamp, _turn, etc.
    }


def _blocks_to_openai_text(blocks: list[dict[str, Any]]) -> str:
    """Convert a list of Anthropic content blocks to a single OpenAI text string."""
    parts: list[str] = []
    for block in blocks:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        # image / other blocks are skipped (OpenAI text-only content field)
    return "\n".join(parts)
