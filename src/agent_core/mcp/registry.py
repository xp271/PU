"""
MCP tool → Tool subclass bridge.

Each MCP tool is wrapped in a dynamically-generated :class:`Tool` subclass
and registered into a *query-scoped* :class:`ToolRegistry` instance under
the name ``mcp__{server}__{tool}``.

The wrapper delegates ``call()`` to ``session.call_tool()`` and uses the
MCP-provided ``inputSchema`` for input validation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, create_model

from ..tools.base import Tool, ToolCallResult, ToolUseContext
from ..tools.registry import ToolRegistry
from .client import McpConnectionState

logger = logging.getLogger("agent_core.mcp")

MCP_TOOL_PREFIX = "mcp__"

# Provider function-name constraint (OpenAI/DeepSeek/Anthropic): ``^[a-zA-Z0-9_-]+$``.
# MCP server names can contain ``@`` / ``/`` (e.g. ``@playwright/mcp``), which are
# illegal — so the namespaced name is sanitised before it ever reaches the LLM.
_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _mcp_tool_name(server: str, tool: str) -> str:
    """Generate the namespaced, provider-safe tool name.

    Base form is ``mcp__{server}__{tool}``; any character outside
    ``[a-zA-Z0-9_-]`` is replaced with ``_`` so the name satisfies the
    LLM function-name pattern.  The substitution is per-character, so the
    ``mcp__{server}__`` prefix used by :func:`_unregister_server_tools`
    stays a valid prefix of the full name.  The *actual* MCP ``call_tool``
    uses the original tool name (stored separately), not this one.
    """
    return _INVALID_NAME_CHARS.sub("_", f"{MCP_TOOL_PREFIX}{server}__{tool}")


def _parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Parse ``mcp__server__tool`` → ``(server, tool)``, or ``None``."""
    if not name.startswith(MCP_TOOL_PREFIX):
        return None
    parts = name.removeprefix(MCP_TOOL_PREFIX).split("__", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def register_mcp_tools(
    registry: ToolRegistry,
    server_name: str,
    state: McpConnectionState,
) -> dict[str, type[Tool]]:
    """Create and register ``Tool`` subclasses for every tool on an MCP server.

    Operates on the *query-scoped* ``registry`` instance.

    Conflict resolution:
    * Built-in tools with the same base name win (retained).
    * Previously registered MCP tools from a different server coexist
      under different namespaced names.
    * Re-registering the **same** server → remove old, register new.
    """
    registered: dict[str, type[Tool]] = {}

    # 1. Remove stale registrations for this server
    _unregister_server_tools(registry, server_name)

    # 2. Build and register each tool
    for tool_def in state.tools:
        tool_name = tool_def["name"]
        namespaced = _mcp_tool_name(server_name, tool_name)

        # Conflict check: built-in (non-namespaced) name collision
        if registry.contains(tool_name):
            logger.warning(
                "MCP tool '%s' from server '%s' shadows built-in tool '%s'. "
                "Built-in wins. MCP tool registered as '%s'.",
                tool_name,
                server_name,
                tool_name,
                namespaced,
            )

        # Check for exact namespaced name collision (different server)
        if registry.contains(namespaced):
            logger.warning(
                "Namespaced tool '%s' already registered. "
                "Overwriting with tool from server '%s'.",
                namespaced,
                server_name,
            )

        # Build the dynamic Tool subclass — bind *state* (not
        # state.session) so a later reconnect that swaps state.session
        # is picked up live.
        tool_cls = _build_mcp_tool_class(
            namespaced=namespaced,
            server_name=server_name,
            tool_def=tool_def,
            state=state,
        )

        try:
            registry.register(tool_cls)
            registered[namespaced] = tool_cls
        except (TypeError, ValueError) as exc:
            logger.error(
                "Failed to register MCP tool '%s': %s", namespaced, exc
            )

    logger.info(
        "Registered %d/%d tools from MCP server '%s'",
        len(registered),
        len(state.tools),
        server_name,
    )
    return registered


# ---------------------------------------------------------------------------
# Dynamic Tool subclass factory
# ---------------------------------------------------------------------------


def _build_mcp_tool_class(
    namespaced: str,
    server_name: str,
    tool_def: dict[str, Any],
    state: McpConnectionState,
) -> type[Tool]:
    """Dynamically create a ``Tool`` subclass wrapping one MCP tool."""

    input_model = _build_input_model(namespaced, tool_def)

    # Resolve session lazily from the live state (survives reconnect)
    def _session_getter() -> Any:
        return state.session

    class_dict = {
        "name": namespaced,
        "is_concurrency_safe": False,
        "is_read_only": False,
        "is_destructive": False,
        "_server_name": server_name,
        "_session_getter": staticmethod(_session_getter),
        "_mcp_tool_name": tool_def["name"],
        "_input_model": input_model,
        "input_schema": classmethod(_make_input_schema(tool_def)),
        "validate_input": classmethod(_make_validate_input(input_model)),
        "call": _make_mcp_call(namespaced),
    }

    safe_name = namespaced.replace("-", "_")
    return type(f"MCPTool_{safe_name}", (Tool,), class_dict)


# ---------------------------------------------------------------------------
# Input model builders
# ---------------------------------------------------------------------------


def _build_input_model(
    namespaced: str,
    tool_def: dict[str, Any],
) -> type[BaseModel]:
    """Convert MCP ``inputSchema`` (JSON Schema) to a Pydantic model.

    Complex schemas (``$ref``, ``oneOf``, ``anyOf``, ``type`` as list)
    fall back to a raw-dict model.
    """
    schema = tool_def.get("inputSchema", {})

    if not schema or schema.get("type") != "object":
        return create_model(
            f"{namespaced}_Input",
            raw=(Any, ...),
        )

    properties = schema.get("properties", {})
    required: set[str] = set(schema.get("required", []))

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _json_type_to_python(prop_schema)
        if prop_name in required:
            fields[prop_name] = (py_type, ...)
        else:
            fields[prop_name] = (Optional[py_type], None)

    if not fields:
        fields["raw"] = (dict[str, Any], ...)

    safe_name = namespaced.replace("-", "_")
    return create_model(f"{safe_name}_Input", **fields)


def _json_type_to_python(prop_schema: dict) -> Any:
    """Map JSON Schema type → Python type for Pydantic."""
    jtype = prop_schema.get("type", "string")
    # JSON Schema allows type to be a list (e.g. ["string", "null"]) → Any
    if isinstance(jtype, list):
        return Any
    type_map: dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return type_map.get(jtype, Any)


# ---------------------------------------------------------------------------
# Callable factories (one per tool — avoids closure bugs)
# ---------------------------------------------------------------------------


def _make_input_schema(tool_def: dict[str, Any]):
    """Build the ``input_schema`` classmethod body for a dynamic MCP tool.

    Returns the MCP-provided ``inputSchema`` (already JSON Schema) so the
    LLM provider's ``convert_tools`` can expose the tool.  Without this the
    tool inherits ``Tool.input_schema`` which raises ``NotImplementedError``.
    Providers (Anthropic/OpenAI) require an *object* schema, so non-object
    or missing schemas fall back to a permissive object.
    """
    schema = tool_def.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}, "additionalProperties": True}

    def _input_schema(cls) -> dict[str, Any]:
        return schema

    return _input_schema


def _make_validate_input(model_cls: type[BaseModel]):
    """Build the ``validate_input`` classmethod body."""

    def _validate(cls, raw: dict[str, Any]) -> BaseModel:
        if "raw" in model_cls.model_fields:
            return model_cls(raw=raw)
        return model_cls(**raw)

    return _validate


def _make_mcp_call(tool_name: str):
    """Create the async ``call()`` method for a dynamic MCP Tool subclass."""

    async def _call(
        self: Tool, input: BaseModel, context: ToolUseContext
    ) -> ToolCallResult:
        session = self._session_getter()  # type: ignore[attr-defined]
        if session is None:
            return ToolCallResult(
                content=f"MCP server disconnected. Cannot call tool '{tool_name}'.",
                is_error=True,
            )

        # Extract arguments dict from the validated input model
        if "raw" in type(input).model_fields:
            args = input.raw  # type: ignore[attr-defined]
        else:
            args = input.model_dump(exclude_none=True)

        try:
            result = await session.call_tool(
                self._mcp_tool_name, args  # type: ignore[attr-defined]
            )

            # MCP returns list[ContentBlock]; extract text
            text_parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                else:
                    text_parts.append(str(block))

            return ToolCallResult(
                content="\n".join(text_parts),
                is_error=getattr(result, "isError", False),
            )

        except Exception as exc:
            return ToolCallResult(
                content=f"MCP tool '{tool_name}' execution error: {exc}",
                is_error=True,
            )

    return _call


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unregister_server_tools(
    registry: ToolRegistry, server_name: str
) -> None:
    """Remove all tools belonging to *server_name* from the registry."""
    prefix = _mcp_tool_name(server_name, "")
    to_remove = [name for name in registry.names() if name.startswith(prefix)]
    for name in to_remove:
        registry.unregister(name)
    if to_remove:
        logger.debug(
            "Unregistered %d tools from MCP server '%s'",
            len(to_remove),
            server_name,
        )
