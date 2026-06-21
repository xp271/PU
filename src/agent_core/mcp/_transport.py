"""
Transport adapters for the ``mcp`` SDK.

Exposes a single :func:`open_session` async-context-manager factory
that the :class:`McpSessionManager` enters via its
:class:`~contextlib.AsyncExitStack`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

if TYPE_CHECKING:
    from ..models import McpServerConfig


@asynccontextmanager
async def open_session(
    config: "McpServerConfig",
    auth_headers: dict[str, str] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Open an MCP session for *config* and yield it.

    The session is yielded **inside** the transport's async-with, so the
    transport (and any stdio subprocess) stays alive for as long as the
    caller holds the session.  The caller enters this via
    ``AsyncExitStack.enter_async_context`` and never exits it manually —
    teardown happens when the stack is closed (same task).

    ``initialize()`` is called exactly once, here.
    """
    match config.transport:
        case "stdio":
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd,
            )
            async with stdio_client(params) as (read, write), ClientSession(
                read, write
            ) as session:
                await session.initialize()
                yield session

        case "sse":
            headers = {**config.headers, **(auth_headers or {})}
            async with sse_client(
                url=config.url, headers=headers
            ) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                yield session

        case "websocket":
            raise NotImplementedError("WebSocket transport not yet supported")

        case _:
            raise ValueError(f"Unknown transport type: {config.transport}")
