"""Mock integration tests for MCP — session manager, registration, isolation."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

from agent_core.models import (
    McpOAuthConfig,
    McpServerConfig,
    McpServerTransport,
    AgentConfig,
    AgentMessage,
)
from agent_core.mcp.client import McpSessionManager, McpConnectionState
from agent_core.mcp.registry import register_mcp_tools
from agent_core.tools.registry import ToolRegistry
from agent_core.tools.base import Tool

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_mock_tool_result(tools: list[dict]) -> MagicMock:
    """Build a mock list_tools() result with .tools attribute."""
    mock_results = []
    for t in tools:
        r = MagicMock()
        r.name = t["name"]
        r.description = t.get("description", "")
        r.inputSchema = t.get("inputSchema", {})
        mock_results.append(r)
    result = MagicMock()
    result.tools = mock_results
    return result


def _mock_session(tools: list[dict] | None = None) -> AsyncMock:
    """Create a mock ClientSession."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(
        return_value=_make_mock_tool_result(tools or [])
    )
    session.call_tool = AsyncMock()
    return session


# ------------------------------------------------------------------
# Mock transport — returns a pre-built session
# ------------------------------------------------------------------


class _MockOpenSessionCtx:
    """Simulates async context manager that yields a mock session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        pass


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestMcpSessionManager:
    async def test_connect_success(self) -> None:
        """connect() enters the session into the stack and returns tools."""
        session = _mock_session([
            {"name": "hello", "description": "Say hello", "inputSchema": {}}
        ])
        mgr = McpSessionManager()

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_MockOpenSessionCtx(session),
        ):
            config = McpServerConfig(command="echo")
            state = await mgr.connect("test", config)

        assert state.connection_error is None
        assert len(state.tools) == 1
        assert state.tools[0]["name"] == "hello"
        assert state.session is session
        assert "test" in mgr.connections
        assert mgr.connections["test"] is state

        await mgr.aclose()

    async def test_connect_timeout_retries(self) -> None:
        """On timeout, retries are attempted."""
        session = _mock_session([])
        call_count = [0]

        class _TimeoutThenOk:
            def __init__(self, s):
                self._s = s

            async def __aenter__(self):
                call_count[0] += 1
                if call_count[0] < 2:
                    raise asyncio.TimeoutError("too slow")
                return self._s

            async def __aexit__(self, *a):
                pass

        mgr = McpSessionManager()

        def _side_effect(*args, **kwargs):
            return _TimeoutThenOk(session)

        with patch("agent_core.mcp.client.open_session", side_effect=_side_effect):
            config = McpServerConfig(command="echo", connect_timeout=1.0, reconnect_delay=0.01)
            state = await mgr.connect("test", config)

        assert state.connection_error is None
        assert call_count[0] == 2  # first timeout, second success
        await mgr.aclose()

    async def test_connect_fatal_no_retry(self) -> None:
        """Fatal errors (401) do not retry."""
        mgr = McpSessionManager()

        class _Raise401:
            async def __aenter__(self):
                exc = Exception("unauthorized")
                exc.response = type("R", (), {"status_code": 401})()  # type: ignore[attr-defined]
                raise exc

            async def __aexit__(self, *a):
                pass

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_Raise401(),
        ):
            config = McpServerConfig(command="echo", reconnect_delay=0.01)
            state = await mgr.connect("test", config)

        assert state.connection_error is not None
        assert state.fatal is True
        # No session entered into stack for fatal errors
        await mgr.aclose()

    async def test_session_survives_across_calls(self) -> None:
        """Session entered via stack stays alive — regression for core bug."""
        session = _mock_session([
            {"name": "t1", "description": "", "inputSchema": {}}
        ])
        mgr = McpSessionManager()

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_MockOpenSessionCtx(session),
        ):
            config = McpServerConfig(command="echo")
            state = await mgr.connect("test", config)

        # Session should still be usable — not garbage collected
        assert state.session is session

        # call_tool should work
        call_result = MagicMock()
        call_result.content = [MagicMock(text="hello")]
        call_result.isError = False
        session.call_tool.return_value = call_result

        result = await session.call_tool("t1", {"x": 1})
        assert result.content[0].text == "hello"

        await mgr.aclose()


class TestMcpToolRegistration:
    async def test_registered_tools_appear_in_registry(self) -> None:
        session = _mock_session([
            {"name": "search", "description": "Search", "inputSchema": {}}
        ])
        mgr = McpSessionManager()
        registry = ToolRegistry()

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_MockOpenSessionCtx(session),
        ):
            state = await mgr.connect("github", McpServerConfig(command="cmd"))
            registered = register_mcp_tools(registry, "github", state)

        assert "mcp__github__search" in registered
        assert registry.contains("mcp__github__search")
        await mgr.aclose()

    async def test_builtin_wins_over_mcp_name_collision(self) -> None:
        """If MCP provides a tool named 'Read', built-in Read is retained."""
        registry = ToolRegistry()
        # Simulate a built-in tool already registered
        from agent_core.tools import FileReadTool
        registry.register(FileReadTool)

        session = _mock_session([
            {"name": "Read", "description": "MCP read", "inputSchema": {}}
        ])
        mgr = McpSessionManager()

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_MockOpenSessionCtx(session),
        ):
            state = await mgr.connect("fs", McpServerConfig(command="cmd"))
            register_mcp_tools(registry, "fs", state)

        # Built-in 'Read' still there
        assert registry.contains("Read")
        # MCP tool registered under namespaced name
        assert registry.contains("mcp__fs__Read")
        await mgr.aclose()


class TestMcpAcloseCleanup:
    async def test_aclose_clears_connections(self) -> None:
        session = _mock_session([])
        mgr = McpSessionManager()

        with patch(
            "agent_core.mcp.client.open_session",
            return_value=_MockOpenSessionCtx(session),
        ):
            await mgr.connect("s1", McpServerConfig(command="cmd1"))
            await mgr.connect("s2", McpServerConfig(command="cmd2"))

        assert len(mgr.connections) == 2
        await mgr.aclose()
        assert len(mgr.connections) == 0


class TestBootstrapSingleFailureIsolation:
    async def test_one_bad_server_does_not_block_others(self) -> None:
        """A failing server must not prevent other servers from connecting."""
        session = _mock_session([
            {"name": "ok_tool", "description": "", "inputSchema": {}}
        ])
        mgr = McpSessionManager()

        class _RaiseFileNotFound:
            async def __aenter__(self):
                raise FileNotFoundError("bad_cmd not found")
            async def __aexit__(self, *a):
                pass

        config_bad = McpServerConfig(command="bad_cmd", reconnect_delay=0.01, max_reconnect_attempts=1)

        def _side_effect(*args, **kwargs):
            # First call returns bad context, second returns good context
            if not hasattr(_side_effect, "called"):
                _side_effect.called = True  # type: ignore[attr-defined]
                return _RaiseFileNotFound()
            return _MockOpenSessionCtx(session)

        with patch("agent_core.mcp.client.open_session", side_effect=_side_effect):
            bad_state = await mgr.connect("bad", config_bad)
            good_state = await mgr.connect("good", McpServerConfig(command="ok"))

        assert bad_state.connection_error is not None
        assert good_state.connection_error is None
        assert len(good_state.tools) == 1
        await mgr.aclose()


class TestEnvVarExpansion:
    async def test_env_var_expansion(self) -> None:
        from agent_core.mcp.config import _expand_env_vars

        os.environ["TEST_MCP_TOKEN"] = "secret123"
        try:
            result = _expand_env_vars("Bearer ${TEST_MCP_TOKEN}")
            assert result == "Bearer secret123"
        finally:
            del os.environ["TEST_MCP_TOKEN"]

    async def test_missing_env_var_raises(self) -> None:
        from agent_core.mcp.config import _expand_env_vars

        with pytest.raises(KeyError, match="NOT_SET_VAR"):
            _expand_env_vars("${NOT_SET_VAR}")

    async def test_nested_expansion(self) -> None:
        from agent_core.mcp.config import _expand_env_vars

        os.environ["TEST_MCP_HOST"] = "example.com"
        os.environ["TEST_MCP_PORT"] = "8080"
        try:
            result = _expand_env_vars({
                "url": "https://${TEST_MCP_HOST}:${TEST_MCP_PORT}/mcp",
                "headers": {"Authorization": "Bearer ${TEST_MCP_HOST}"},
            })
            assert result["url"] == "https://example.com:8080/mcp"
            assert result["headers"]["Authorization"] == "Bearer example.com"
        finally:
            del os.environ["TEST_MCP_HOST"]
            del os.environ["TEST_MCP_PORT"]


class TestOAuthHeaderInjection:
    async def test_oauth_headers_injected_into_transport(self) -> None:
        """When OAuth is configured, Bearer token is passed to open_session."""
        received_auth = []

        def _capture_auth(config, auth_headers=None):
            received_auth.append(auth_headers)
            session = _mock_session([])
            return _MockOpenSessionCtx(session)

        mgr = McpSessionManager()
        auth = McpOAuthConfig(
            client_id="id",
            client_secret="secret",
            token_url="https://example.com/token",
            token="test-token-abc",
            expires_at=9999999999.0,
        )
        config = McpServerConfig(
            transport=McpServerTransport.SSE,
            url="https://example.com/mcp",
            auth=auth,
        )

        with patch("agent_core.mcp.client.open_session", side_effect=_capture_auth):
            await mgr.connect("srv", config)

        assert received_auth[0] == {"Authorization": "Bearer test-token-abc"}
        await mgr.aclose()


class TestConcurrentIsolation:
    async def test_two_concurrent_queries_isolated(self) -> None:
        """Two concurrent queries must not share registry or connections."""
        r1 = ToolRegistry()
        r2 = ToolRegistry()

        session1 = _mock_session([{"name": "a", "description": "", "inputSchema": {}}])
        session2 = _mock_session([{"name": "b", "description": "", "inputSchema": {}}])

        mgr1 = McpSessionManager()
        mgr2 = McpSessionManager()

        with patch(
            "agent_core.mcp.client.open_session",
            side_effect=[
                _MockOpenSessionCtx(session1),
                _MockOpenSessionCtx(session2),
            ],
        ):
            s1 = await mgr1.connect("srv1", McpServerConfig(command="c1"))
            s2 = await mgr2.connect("srv2", McpServerConfig(command="c2"))
            register_mcp_tools(r1, "srv1", s1)
            register_mcp_tools(r2, "srv2", s2)

        assert r1.contains("mcp__srv1__a")
        assert not r1.contains("mcp__srv2__b")
        assert r2.contains("mcp__srv2__b")
        assert not r2.contains("mcp__srv1__a")

        assert "srv1" in mgr1.connections
        assert "srv2" not in mgr1.connections
        assert "srv2" in mgr2.connections
        assert "srv1" not in mgr2.connections

        await mgr1.aclose()
        await mgr2.aclose()
