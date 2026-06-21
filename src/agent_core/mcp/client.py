"""
MCP connection management — owns all sessions for one ``query()`` call.

:class:`McpSessionManager` uses a single :class:`~contextlib.AsyncExitStack`
to enter every transport/session pair.  ``connect()`` and ``aclose()`` MUST
run in the same asyncio task (anyio cancel-scope requirement).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from ._transport import open_session
from .auth import OAuthTokenManager
from ..models import McpServerConfig, McpServerTransport

logger = logging.getLogger("agent_core.mcp")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class McpConnectionState:
    """Runtime state for an active MCP server connection."""

    server_name: str
    config: McpServerConfig
    session: Any = None  # ``mcp.ClientSession`` (duck-typed to avoid import)
    tools: list[dict[str, Any]] = field(default_factory=list)
    connected_at: float = 0.0
    connection_error: str | None = None
    fatal: bool = False


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class McpSessionManager:
    """Owns all MCP connections for one ``query()`` invocation.

    All sessions are entered through a single ``AsyncExitStack``.
    ``connect()`` and ``aclose()`` MUST execute in the same task
    (anyio cancel-scope requirement) — do **not** wrap ``connect()``
    in an outer ``asyncio.timeout`` that could cancel across the
    stack boundary.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.connections: dict[str, McpConnectionState] = {}

    # -- connect ----------------------------------------------------------

    async def connect(
        self, name: str, config: McpServerConfig
    ) -> McpConnectionState:
        """Connect to one server, entering its session into the stack.

        On success the session stays alive until ``aclose()``.  On failure
        the returned state carries ``connection_error`` plus the ``fatal``
        flag, and nothing is left on the stack for this server.
        """
        state = McpConnectionState(server_name=name, config=config)

        # Resolve OAuth headers up front (if configured).
        auth_headers: dict[str, str] | None = None
        if config.auth is not None:
            token = await OAuthTokenManager(config.auth).get_valid_token()
            if token:
                auth_headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(config.max_reconnect_attempts + 1):
            try:
                # Per-attempt timeout lives *inside* this task; it guards
                # only connect+handshake, not the long-lived session.
                async with asyncio.timeout(config.connect_timeout):
                    session = await self._stack.enter_async_context(
                        open_session(config, auth_headers)
                    )
                    result = await session.list_tools()

                state.session = session
                state.tools = _tools_from_result(result, config)
                state.connected_at = _time.monotonic()
                state.connection_error = None
                state.fatal = False
                self.connections[name] = state
                logger.info(
                    "Connected to MCP server '%s' (%s): %d tools",
                    name,
                    config.transport.value,
                    len(state.tools),
                )
                return state

            except asyncio.TimeoutError:
                msg = (
                    f"Connection to '{name}' timed out after "
                    f"{config.connect_timeout}s"
                )
                logger.warning(msg)
                state.connection_error, state.fatal = msg, False

            except Exception as exc:
                msg = f"Failed to connect to MCP server '{name}': {exc}"
                is_fatal = _is_fatal_error(exc)
                if is_fatal or attempt >= config.max_reconnect_attempts:
                    logger.error(msg)
                    state.connection_error, state.fatal = msg, is_fatal
                    break
                delay = config.reconnect_delay * (2**attempt)
                logger.warning(
                    "%s — retrying in %.1fs (attempt %d/%d)",
                    msg,
                    delay,
                    attempt + 1,
                    config.max_reconnect_attempts,
                )
                await asyncio.sleep(delay)

        self.connections[name] = state
        return state

    # -- refresh ----------------------------------------------------------

    async def refresh_tools(self, name: str) -> list[dict[str, Any]]:
        """Re-list tools (e.g. on ``notifications/list_changed``)."""
        state = self.connections.get(name)
        if state is None or state.session is None:
            raise RuntimeError(f"MCP server '{name}' is not connected")
        result = await state.session.list_tools()
        state.tools = _tools_from_result(result, state.config)
        return state.tools

    # -- teardown ---------------------------------------------------------

    async def aclose(self) -> None:
        """Tear down every session/transport.

        Must run in the **same task** that called ``connect()``.
        """
        try:
            await self._stack.aclose()
        except Exception as exc:
            logger.warning("Error during MCP teardown: %s", exc)
        finally:
            self.connections.clear()
            logger.info("All MCP connections closed")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tools_from_result(result, config: McpServerConfig) -> list[dict[str, Any]]:
    tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema,
        }
        for t in result.tools
    ]
    return _filter_tools(tools, config.include_tools, config.exclude_tools)


def _filter_tools(
    tools: list[dict[str, Any]],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[dict[str, Any]]:
    """Apply regex include/exclude filters to tool list."""
    result = tools
    if include:
        patterns = [re.compile(p) for p in include]
        result = [t for t in result if any(p.search(t["name"]) for p in patterns)]
    if exclude:
        patterns = [re.compile(p) for p in exclude]
        result = [t for t in result if not any(p.search(t["name"]) for p in patterns)]
    return result


def _is_fatal_error(exc: Exception) -> bool:
    """Determine if a *connection* error is fatal (no retry).

    Prefers typed/status-based checks over substring matching.
    """
    # 1. HTTP status (httpx / SDK errors often carry .response.status_code)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    if status is not None and 500 <= status < 600:
        return False  # server-side, worth retrying

    # 2. Config/transport errors that can never succeed on retry
    if isinstance(exc, (NotImplementedError, FileNotFoundError, ValueError)):
        return True

    # 3. Narrow auth keyword fallback
    msg = str(exc).lower()
    return any(
        kw in msg for kw in ("unauthorized", "forbidden", "invalid_grant", "invalid_client")
    )
