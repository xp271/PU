"""
Convert the Tool registry's JSON Schemas into provider-native tool-declaration
formats consumed by the Anthropic Messages API and OpenAI Chat Completions API.
"""

from __future__ import annotations

from typing import Any

from ..tools.registry import registered_tools


def tools_for_anthropic(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """Generate Anthropic Messages API ``tools`` parameter format.

    Parameters
    ----------
    allowed_tools:
        List of tool names to include.  ``["*"]`` means *all* registered tools.
    """
    tools: list[dict[str, Any]] = []
    for name, cls in registered_tools().items():
        if "*" not in allowed_tools and name not in allowed_tools:
            continue
        tools.append({
            "name": name,
            "description": (cls.__doc__ or "").strip(),
            "input_schema": cls.input_schema(),
        })
    return tools


def tools_for_openai(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """Generate OpenAI Chat Completions API ``tools`` parameter format.

    Parameters
    ----------
    allowed_tools:
        List of tool names to include.  ``["*"]`` means *all* registered tools.
    """
    tools: list[dict[str, Any]] = []
    for name, cls in registered_tools().items():
        if "*" not in allowed_tools and name not in allowed_tools:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (cls.__doc__ or "").strip(),
                "parameters": cls.input_schema(),
            },
        })
    return tools
