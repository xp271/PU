"""MCP (Model Context Protocol) integration.

Provides :class:`McpSessionManager` (connection lifecycle),
dynamic tool registration into a query-scoped :class:`ToolRegistry`,
OAuth authentication, and configuration-loading helpers.
"""

from __future__ import annotations

from .client import McpSessionManager
from .registry import register_mcp_tools

__all__ = [
    "McpSessionManager",
    "register_mcp_tools",
]
