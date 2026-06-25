"""
Unit tests for the plugin system (Item 9):
- Plugin base class
- PluginRegistry (registration, discovery, enable/disable)
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from agent_core.plugins.base import Plugin
from agent_core.plugins.registry import PluginRegistry
from agent_core.hooks.registry import HookRegistry


# ============================================================================
# Minimal concrete plugin for testing
# ============================================================================


class TestTool:
    name: ClassVar[str] = "TestTool"


class _TestPlugin(Plugin):
    name: ClassVar[str] = "test-plugin"

    def get_tools(self):
        return [TestTool]

    def get_hooks(self):
        from tests.unit.test_hooks import _make_hook
        return [_make_hook("pre_tool_use")]


# ============================================================================
# Plugin base class
# ============================================================================


class TestPluginBase:
    def test_defaults(self):
        plugin = _TestPlugin()
        assert plugin.name == "test-plugin"
        assert plugin.version == "0.1.0"

    def test_default_get_tools(self):
        class Minimal(Plugin):
            name: ClassVar[str] = "minimal"
        assert Minimal().get_tools() == []

    def test_default_get_hooks(self):
        class Minimal(Plugin):
            name: ClassVar[str] = "minimal"
        assert Minimal().get_hooks() == []

    def test_default_get_agent_definitions(self):
        class Minimal(Plugin):
            name: ClassVar[str] = "minimal"
        assert Minimal().get_agent_definitions() == []

    def test_default_get_skills(self):
        class Minimal(Plugin):
            name: ClassVar[str] = "minimal"
        assert Minimal().get_skills() == []

    def test_on_enable_is_async(self):
        class Minimal(Plugin):
            name: ClassVar[str] = "m"
        import asyncio
        assert asyncio.iscoroutinefunction(Minimal().on_enable)


# ============================================================================
# PluginRegistry — registration & lifecycle
# ============================================================================


class TestRegistryRegister:
    def test_register_plugin(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)
        assert "test-plugin" in registry.available_plugins()

    def test_register_missing_name_raises(self):
        registry = PluginRegistry()

        class BadPlugin(Plugin):
            pass  # no name

        with pytest.raises(ValueError, match="must define a 'name'"):
            registry.register(BadPlugin)


class TestRegistryEnableDisable:
    @pytest.mark.asyncio
    async def test_enable_injects_tools(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)

        from agent_core.tools.registry import ToolRegistry
        tool_reg = ToolRegistry()
        await registry.enable("test-plugin", tool_registry=tool_reg)
        assert "TestTool" in tool_reg.names()

    @pytest.mark.asyncio
    async def test_enable_injects_hooks(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)

        hook_reg = HookRegistry()
        await registry.enable("test-plugin", hook_registry=hook_reg)
        assert not hook_reg.is_empty()
        assert len(hook_reg.pre_tool_use_hooks()) == 1

    @pytest.mark.asyncio
    async def test_enable_is_idempotent(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)

        hook_reg = HookRegistry()
        await registry.enable("test-plugin", hook_registry=hook_reg)
        count = len(hook_reg.pre_tool_use_hooks())
        await registry.enable("test-plugin", hook_registry=hook_reg)
        # Should not double-inject
        assert len(hook_reg.pre_tool_use_hooks()) == count

    @pytest.mark.asyncio
    async def test_disable_removes_from_enabled(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)

        await registry.enable("test-plugin")
        assert registry.is_enabled("test-plugin")

        await registry.disable("test-plugin")
        assert not registry.is_enabled("test-plugin")

    @pytest.mark.asyncio
    async def test_enable_unknown_raises(self):
        registry = PluginRegistry()
        with pytest.raises(KeyError, match="Unknown plugin"):
            await registry.enable("nonexistent")

    @pytest.mark.asyncio
    async def test_unregister_while_enabled_raises(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)
        await registry.enable("test-plugin")
        with pytest.raises(RuntimeError, match="currently enabled"):
            registry.unregister("test-plugin")


class TestRegistryQueries:
    def test_is_enabled(self):
        registry = PluginRegistry()
        assert not registry.is_enabled("any")

    def test_enabled_plugins(self):
        registry = PluginRegistry()
        assert registry.enabled_plugins() == []

    def test_available_plugins(self):
        registry = PluginRegistry()
        registry.register(_TestPlugin)
        assert "test-plugin" in registry.available_plugins()

    def test_get(self):
        registry = PluginRegistry()
        assert registry.get("no") is None
