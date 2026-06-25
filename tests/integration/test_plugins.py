"""
Integration test: plugin contributions reach a running ``query()``.

Verifies the Item-9 wiring — ``query()`` discovers plugins and auto-enables
the names in ``config.auto_enable_plugins``, injecting their hooks/skills into
the *per-query* registries.  Here a plugin contributes a PreToolUse hook that
blocks a tool; the effect is observable in the tool_result.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from agent_core.hooks.types import HookBlock
from agent_core.models import AgentConfig, AgentMessage
from agent_core.plugins.base import Plugin
from agent_core.plugins.registry import PluginRegistry
from agent_core.query.loop import query
from tests.integration.mock_model import MockClient, make_tool_use_turn


def _collect(agen) -> list[AgentMessage]:
    async def _drain():
        out: list[AgentMessage] = []
        async for msg in agen:
            out.append(msg)
        return out

    return asyncio.run(_drain())


def _mock_factory(client: MockClient):
    def _factory(_model_id, _config):
        return client

    return _factory


class _BlockHook:
    hook_kind = "pre_tool_use"

    async def __call__(self, tool_name, tool_input, context):
        return HookBlock(reason="blocked-by-plugin")


class _BlockingPlugin(Plugin):
    name = "blocking-plugin"

    def get_hooks(self) -> list[Any]:
        return [_BlockHook()]


def test_plugin_hook_reaches_query() -> None:
    # Make the per-query PluginRegistry.discover() surface our plugin without
    # needing a real installed entry point.
    def _fake_discover(self: PluginRegistry) -> list[str]:
        self.register(_BlockingPlugin)
        return [_BlockingPlugin.name]

    client = MockClient(make_tool_use_turn("Bash", {"command": "echo hi"}))
    config = AgentConfig(
        max_turns=1,
        permission_mode="bypassPermissions",
        auto_enable_plugins=["blocking-plugin"],
    )

    with patch.object(PluginRegistry, "discover", _fake_discover), patch(
        "agent_core.query.loop.get_client", _mock_factory(client)
    ):
        msgs = _collect(query("go", config, trace_id="p1"))

    results = [m for m in msgs if m.type == "tool_result"]
    assert results, "expected a tool_result"
    assert results[0].is_error
    assert "blocked-by-plugin" in results[0].content


def test_no_auto_enable_means_no_plugin_effect() -> None:
    """Without auto_enable_plugins, discover/enable is never invoked."""
    called = {"discover": False}

    def _spy_discover(self: PluginRegistry) -> list[str]:
        called["discover"] = True
        return []

    client = MockClient(make_tool_use_turn("Bash", {"command": "echo hi"}))
    config = AgentConfig(max_turns=1, permission_mode="bypassPermissions")

    with patch.object(PluginRegistry, "discover", _spy_discover), patch(
        "agent_core.query.loop.get_client", _mock_factory(client)
    ):
        msgs = _collect(query("go", config, trace_id="p2"))

    assert called["discover"] is False
    results = [m for m in msgs if m.type == "tool_result"]
    assert results and not results[0].is_error
