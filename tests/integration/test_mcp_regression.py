"""Regression tests for MCP-integration bugs found while wiring up a
web-search MCP server.

Three distinct bugs are pinned here:

1. **NotImplementedError on every query.**  The dynamically-built MCP Tool
   subclass forgot to define ``input_schema()``, so ``convert_tools`` (called
   at the start of *every* query) hit the base ``Tool.input_schema`` stub.

2. **Invalid provider function name (HTTP 400).**  Server names like
   ``@playwright/mcp`` produced tool names with ``@`` / ``/``, which violate
   the OpenAI/DeepSeek ``^[a-zA-Z0-9_-]+$`` pattern.

3. **"exit cancel scope in a different task" at shutdown.**  ``query()`` had
   early-return paths that skipped ``mcp.aclose()``, leaking anyio cancel
   scopes to ``asyncio.run()`` shutdown.  ``aclose()`` must run once, in-task,
   on every exit path — including consumer abandonment.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import patch

import pytest

from agent_core.llm.deepseek import DeepSeekClient
from agent_core.mcp.client import McpConnectionState
from agent_core.mcp.registry import (
    _build_mcp_tool_class,
    _mcp_tool_name,
    register_mcp_tools,
)
from agent_core.models import AgentConfig, AgentMessage, McpServerConfig
from agent_core.query.loop import query
from agent_core.tools.base import Tool
from agent_core.tools.registry import ToolRegistry
from tests.integration.mock_model import (
    MockClient,
    make_error_turn,
    make_text_turn,
    make_tool_use_turn,
)

# Provider function-name constraint shared by OpenAI / DeepSeek / Anthropic.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _make_state(server: str, tool: str, schema: dict | None) -> McpConnectionState:
    """Build a connection state carrying one tool definition."""
    state = McpConnectionState(
        server_name=server, config=McpServerConfig(command="x")
    )
    state.tools = [{"name": tool, "description": "a tool", "inputSchema": schema}]
    return state


# ---------------------------------------------------------------------------
# Bug 1 — input_schema must exist (no NotImplementedError)
# ---------------------------------------------------------------------------


class TestInputSchema:
    def test_input_schema_returns_server_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        tool_def = {"name": "search", "description": "web search", "inputSchema": schema}
        state = _make_state("ddg", "search", schema)
        cls = _build_mcp_tool_class("mcp__ddg__search", "ddg", tool_def, state)

        # The critical regression: this used to raise NotImplementedError.
        assert cls.input_schema() == schema

    def test_input_schema_non_object_falls_back_to_object(self) -> None:
        tool_def = {"name": "ping", "description": "", "inputSchema": None}
        state = _make_state("svc", "ping", None)
        cls = _build_mcp_tool_class("mcp__svc__ping", "svc", tool_def, state)

        out = cls.input_schema()
        assert out["type"] == "object"  # providers require an object schema

    def test_base_tool_input_schema_still_raises(self) -> None:
        # Guard the assumption that the base stub is what we're shadowing.
        with pytest.raises(NotImplementedError):
            Tool.input_schema()

    def test_convert_tools_does_not_raise_for_mcp_tool(self) -> None:
        """End-to-end: the schema build that runs on every query succeeds."""
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        state = _make_state("ddg", "search", schema)
        registry = ToolRegistry()
        register_mcp_tools(registry, "ddg", state)

        tools = DeepSeekClient.convert_tools(["*"], registry=registry)
        assert len(tools) == 1
        assert tools[0]["function"]["parameters"] == schema


# ---------------------------------------------------------------------------
# Bug 2 — namespaced tool names must satisfy the provider pattern
# ---------------------------------------------------------------------------


class TestToolNameSanitisation:
    def test_special_chars_replaced(self) -> None:
        name = _mcp_tool_name("@playwright/mcp", "browser_navigate")
        assert name == "mcp___playwright_mcp__browser_navigate"
        assert _NAME_PATTERN.match(name)

    def test_clean_names_unchanged(self) -> None:
        assert _mcp_tool_name("github", "list_issues") == "mcp__github__list_issues"
        assert _mcp_tool_name("duckduckgo-search", "search") == (
            "mcp__duckduckgo-search__search"
        )

    def test_unregister_prefix_still_matches_after_sanitise(self) -> None:
        """Sanitisation is per-char, so the unregister prefix stays valid."""
        schema = {"type": "object", "properties": {}}
        state = _make_state("@playwright/mcp", "browser_click", schema)
        registry = ToolRegistry()

        register_mcp_tools(registry, "@playwright/mcp", state)
        assert any(n.startswith("mcp___playwright_mcp__") for n in registry.names())

        # Re-registering the same server must clean up the old entries.
        register_mcp_tools(registry, "@playwright/mcp", state)
        assert len([n for n in registry.names() if "browser_click" in n]) == 1

    def test_convert_tools_emits_valid_function_name(self) -> None:
        """The exact failure: tools[N].function.name matched the pattern."""
        schema = {"type": "object", "properties": {}}
        state = _make_state("@playwright/mcp", "browser_navigate", schema)
        registry = ToolRegistry()
        register_mcp_tools(registry, "@playwright/mcp", state)

        tools = DeepSeekClient.convert_tools(["*"], registry=registry)
        assert tools, "expected the MCP tool to be emitted"
        for t in tools:
            assert _NAME_PATTERN.match(t["function"]["name"]), t["function"]["name"]


# ---------------------------------------------------------------------------
# Bug 3 — mcp.aclose() runs exactly once on every exit path
# ---------------------------------------------------------------------------


class _SpyMcpManager:
    """Stand-in for McpSessionManager that records aclose() calls.

    ``connect`` returns a state with a ``connection_error`` so tool
    registration is skipped — the test only cares about teardown.
    """

    instances: list["_SpyMcpManager"] = []

    def __init__(self) -> None:
        self.connect_calls: list[str] = []
        self.aclose_calls = 0
        _SpyMcpManager.instances.append(self)

    async def connect(self, name: str, config: Any) -> McpConnectionState:
        self.connect_calls.append(name)
        state = McpConnectionState(server_name=name, config=config)
        state.connection_error = "spy: not really connecting"
        return state

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _run(coro):
    return asyncio.run(coro)


def _config_with_server(**kwargs: Any) -> AgentConfig:
    return AgentConfig(
        mcp_servers={"ddg": McpServerConfig(command="x")},  # type: ignore[arg-type]
        **kwargs,
    )


class TestMcpLifecycle:
    @pytest.fixture(autouse=True)
    def _reset_spy(self):
        _SpyMcpManager.instances = []
        yield
        _SpyMcpManager.instances = []

    @pytest.mark.parametrize(
        "client_factory, max_turns",
        [
            (lambda: MockClient(make_text_turn("hi")), 5),          # no-tool path
            (lambda: MockClient(make_error_turn("overloaded")), 5),  # error path
            (lambda: MockClient(make_tool_use_turn("Read", {"file_path": "/f"})), 2),  # max_turns
        ],
        ids=["text_path", "error_path", "max_turns_path"],
    )
    def test_aclose_called_once_on_each_exit_path(
        self, client_factory, max_turns
    ) -> None:
        client = client_factory()
        config = _config_with_server(
            max_turns=max_turns, permission_mode="bypassPermissions"
        )

        async def _drain() -> list[AgentMessage]:
            out: list[AgentMessage] = []
            async for msg in query("test", config, trace_id="t"):
                out.append(msg)
            return out

        with patch(
            "agent_core.query.loop.McpSessionManager", _SpyMcpManager
        ), patch(
            "agent_core.query.loop.get_client", lambda *_: client
        ):
            msgs = _run(_drain())

        assert msgs[-1].type == "done"
        assert len(_SpyMcpManager.instances) == 1
        spy = _SpyMcpManager.instances[0]
        assert spy.connect_calls == ["ddg"]
        assert spy.aclose_calls == 1, "aclose must run exactly once"

    def test_aclose_runs_when_consumer_abandons_stream(self) -> None:
        """The original crash: a half-consumed generator must still tear down
        MCP in-task (via aclose) instead of leaking to loop shutdown."""
        client = MockClient(
            make_tool_use_turn("Read", {"file_path": "/f"}, text="working")
        )
        config = _config_with_server(
            max_turns=5, permission_mode="bypassPermissions"
        )

        async def _partial() -> None:
            gen = query("test", config, trace_id="t")
            # Pull a couple of messages, then abandon mid-stream.
            await gen.__anext__()
            await gen.aclose()  # GeneratorExit → query's finally → mcp.aclose()

        with patch(
            "agent_core.query.loop.McpSessionManager", _SpyMcpManager
        ), patch(
            "agent_core.query.loop.get_client", lambda *_: client
        ):
            _run(_partial())

        assert len(_SpyMcpManager.instances) == 1
        assert _SpyMcpManager.instances[0].aclose_calls == 1
