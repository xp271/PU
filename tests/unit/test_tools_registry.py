from __future__ import annotations

from typing import ClassVar

import pytest

from agent_core.tools import (
    Tool,
    ToolCallResult,
    get_tool,
    register_tool,
    registered_tools,
)
from agent_core.tools.registry import _registry as _raw_registry


@pytest.mark.registry
class TestToolRegistry:
    def test_register_tool_decorator(self) -> None:
        @register_tool
        class _TestTool(Tool):
            name: ClassVar[str] = "TestRegistry"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        assert "TestRegistry" in _raw_registry
        assert _raw_registry["TestRegistry"] is _TestTool

    def test_register_tool_requires_name(self) -> None:
        with pytest.raises(TypeError, match="must define a 'name'"):
            @register_tool
            class _BadTool(Tool):
                async def call(self, input, context):
                    return ToolCallResult(content="ok")

                def validate_input(self, input):
                    return {}

    def test_get_tool_returns_instance(self) -> None:
        tool = get_tool("TestRegistry")
        assert isinstance(tool, Tool)
        assert tool.name == "TestRegistry"

    def test_get_tool_unknown_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="not registered"):
            get_tool("NonExistentTool")

    def test_registered_tools_returns_copy(self) -> None:
        registered = registered_tools()
        assert "TestRegistry" in registered

    def test_get_tool_returns_same_instance(self) -> None:
        t1 = get_tool("TestRegistry")
        t2 = get_tool("TestRegistry")
        assert t1 is t2

    def test_re_register_evicts_cached_instance(self) -> None:
        old_tool = get_tool("TestRegistry")

        @register_tool
        class _TestToolRereg(Tool):
            name: ClassVar[str] = "TestRegistry"
            async def call(self, input, context):
                return ToolCallResult(content="re-registered")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        new_tool = get_tool("TestRegistry")
        assert type(new_tool) is _TestToolRereg
        assert new_tool is not old_tool


@pytest.mark.registry
class TestToolBase:
    def test_tool_classvars_defaults(self) -> None:
        class _Minimal(Tool):
            name: ClassVar[str] = "Minimal"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        assert _Minimal.is_concurrency_safe is False
        assert _Minimal.is_read_only is False
        assert _Minimal.is_destructive is False

    def test_output_schema_default(self) -> None:
        class _T(Tool):
            name: ClassVar[str] = "T"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        schema = _T.output_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema
