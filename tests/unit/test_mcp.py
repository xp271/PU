"""Unit tests for MCP integration — models, naming, filtering, validation."""

from __future__ import annotations

import pytest

from agent_core.models import (
    McpOAuthConfig,
    McpServerConfig,
    McpServerTransport,
)
from agent_core.mcp.client import _filter_tools, _is_fatal_error
from agent_core.mcp.registry import (
    _build_input_model,
    _json_type_to_python,
    _make_validate_input,
    _mcp_tool_name,
    _parse_mcp_tool_name,
    register_mcp_tools,
    _unregister_server_tools,
)
from agent_core.tools.registry import ToolRegistry


# ------------------------------------------------------------------
# McpServerConfig validation
# ------------------------------------------------------------------


@pytest.mark.model
class TestMcpServerConfig:
    def test_stdio_requires_command(self) -> None:
        with pytest.raises(ValueError, match="'command' is required"):
            McpServerConfig(transport=McpServerTransport.STDIO)

    def test_stdio_accepts_valid_config(self) -> None:
        cfg = McpServerConfig(
            transport=McpServerTransport.STDIO,
            command="echo",
            args=["hello"],
        )
        assert cfg.command == "echo"
        assert cfg.args == ["hello"]

    def test_sse_requires_url(self) -> None:
        with pytest.raises(ValueError, match="'url' is required"):
            McpServerConfig(transport=McpServerTransport.SSE)

    def test_sse_accepts_valid_config(self) -> None:
        cfg = McpServerConfig(
            transport=McpServerTransport.SSE,
            url="https://example.com/mcp",
        )
        assert cfg.url == "https://example.com/mcp"

    def test_websocket_requires_url(self) -> None:
        with pytest.raises(ValueError, match="'url' is required"):
            McpServerConfig(transport=McpServerTransport.WEBSOCKET)


# ------------------------------------------------------------------
# McpOAuthConfig
# ------------------------------------------------------------------


@pytest.mark.model
class TestMcpOAuthConfig:
    def test_minimal_config(self) -> None:
        cfg = McpOAuthConfig(
            client_id="test-client",
            token_url="https://example.com/token",
        )
        assert cfg.client_id == "test-client"
        assert cfg.client_secret is None
        assert cfg.token is None

    def test_full_config(self) -> None:
        cfg = McpOAuthConfig(
            client_id="id",
            client_secret="secret",
            token_url="https://example.com/token",
            scopes=["read", "write"],
            expires_at=1700000000.0,
        )
        assert cfg.scopes == ["read", "write"]
        assert cfg.expires_at == 1700000000.0


# ------------------------------------------------------------------
# Naming helpers
# ------------------------------------------------------------------


class TestMCPNaming:
    def test_mcp_tool_name(self) -> None:
        assert _mcp_tool_name("github", "list_issues") == "mcp__github__list_issues"

    def test_parse_mcp_tool_name_valid(self) -> None:
        assert _parse_mcp_tool_name("mcp__github__list_issues") == ("github", "list_issues")

    def test_parse_mcp_tool_name_not_mcp(self) -> None:
        assert _parse_mcp_tool_name("Read") is None

    def test_parse_mcp_tool_name_empty(self) -> None:
        assert _parse_mcp_tool_name("") is None

    def test_parse_mcp_tool_name_single_underscore(self) -> None:
        assert _parse_mcp_tool_name("mcp__server") is None


# ------------------------------------------------------------------
# Tool filtering
# ------------------------------------------------------------------


class TestFilterTools:
    def make_tools(self, *names: str) -> list[dict]:
        return [
            {"name": n, "description": "", "inputSchema": {}}
            for n in names
        ]

    def test_no_filters_returns_all(self) -> None:
        tools = self.make_tools("a", "b", "c")
        result = _filter_tools(tools, None, None)
        assert len(result) == 3

    def test_include_filter(self) -> None:
        tools = self.make_tools("read", "write", "delete")
        result = _filter_tools(tools, ["read", "write"], None)
        assert [t["name"] for t in result] == ["read", "write"]

    def test_exclude_filter(self) -> None:
        tools = self.make_tools("read", "write", "delete")
        result = _filter_tools(tools, None, ["delete"])
        assert [t["name"] for t in result] == ["read", "write"]

    def test_include_and_exclude(self) -> None:
        tools = self.make_tools("read", "write", "delete", "read_all")
        # Use anchored patterns to avoid "read" matching "read_all"
        result = _filter_tools(tools, ["^read$", "^write$"], ["delete"])
        assert [t["name"] for t in result] == ["read", "write"]

    def test_empty_include_means_allow_all(self) -> None:
        tools = self.make_tools("a", "b")
        result = _filter_tools(tools, [], None)
        assert len(result) == 2

    def test_empty_exclude_means_none(self) -> None:
        tools = self.make_tools("a", "b")
        result = _filter_tools(tools, None, [])
        assert len(result) == 2


# ------------------------------------------------------------------
# Input model building
# ------------------------------------------------------------------


class TestBuildInputModel:
    def test_simple_properties(self) -> None:
        tool_def = {
            "name": "test",
            "description": "",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["path"],
            },
        }
        model = _build_input_model("test__tool", tool_def)
        # required field
        inst = model(path="/tmp")
        assert inst.path == "/tmp"
        # optional field defaults to None
        assert inst.count is None

    def test_no_schema_falls_back_to_raw_dict(self) -> None:
        tool_def = {"name": "test", "description": ""}
        model = _build_input_model("test__tool", tool_def)
        inst = model(raw={"key": "value"})
        assert inst.raw == {"key": "value"}

    def test_non_object_type_falls_back(self) -> None:
        tool_def = {
            "name": "test",
            "description": "",
            "inputSchema": {"type": "array"},
        }
        model = _build_input_model("test__tool", tool_def)
        inst = model(raw=[1, 2, 3])
        assert inst.raw == [1, 2, 3]

    def test_empty_properties_falls_back(self) -> None:
        tool_def = {
            "name": "test",
            "description": "",
            "inputSchema": {"type": "object"},
        }
        model = _build_input_model("test__tool", tool_def)
        inst = model(raw={"a": 1})
        assert inst.raw == {"a": 1}

    def test_optional_field_accepts_none(self) -> None:
        """Non-required fields with Optional wrapper should accept None."""
        tool_def = {
            "name": "test",
            "description": "",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "count": {"type": "integer"},
                },
            },
        }
        model = _build_input_model("test__optional", tool_def)
        # count is not required — should be Optional[int] = None
        inst = model()  # no fields at all
        assert inst.path is None
        assert inst.count is None


# ------------------------------------------------------------------
# JSON type mapping
# ------------------------------------------------------------------


class TestJSONTypeToPython:
    def test_string(self) -> None:
        assert _json_type_to_python({"type": "string"}) is str

    def test_integer(self) -> None:
        assert _json_type_to_python({"type": "integer"}) is int

    def test_number(self) -> None:
        assert _json_type_to_python({"type": "number"}) is float

    def test_boolean(self) -> None:
        assert _json_type_to_python({"type": "boolean"}) is bool

    def test_array(self) -> None:
        assert _json_type_to_python({"type": "array"}) is list

    def test_object(self) -> None:
        assert _json_type_to_python({"type": "object"}) is dict

    def test_unknown_falls_back_to_any(self) -> None:
        from typing import Any
        assert _json_type_to_python({"type": "custom"}) is Any

    def test_list_type_is_any(self) -> None:
        from typing import Any
        assert _json_type_to_python({"type": ["string", "null"]}) is Any


# ------------------------------------------------------------------
# validate_input factory
# ------------------------------------------------------------------


class TestMakeValidateInput:
    def test_validates_correct_input(self) -> None:
        tool_def = {
            "name": "test",
            "description": "",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
        model = _build_input_model("test__validate", tool_def)
        validate = _make_validate_input(model)
        inst = validate(None, {"name": "hello"})  # None = cls (ignored in closure)
        assert inst.name == "hello"

    def test_raw_dict_fallback(self) -> None:
        tool_def = {"name": "test", "description": ""}
        model = _build_input_model("test__raw", tool_def)
        validate = _make_validate_input(model)
        inst = validate(None, {"x": 1, "y": 2})
        assert inst.raw == {"x": 1, "y": 2}


# ------------------------------------------------------------------
# Tool registration
# ------------------------------------------------------------------


class TestRegisterMcpTools:
    def test_registers_tools_in_passed_registry(self) -> None:
        from dataclasses import dataclass, field
        from agent_core.mcp.client import McpConnectionState

        registry = ToolRegistry()
        state = McpConnectionState(
            server_name="test",
            config=McpServerConfig(command="echo"),
            tools=[
                {
                    "name": "hello",
                    "description": "Say hello",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                    },
                },
            ],
        )

        registered = register_mcp_tools(registry, "test", state)
        assert "mcp__test__hello" in registered
        assert registry.contains("mcp__test__hello")

    def test_unregister_cleans_stale_tools(self) -> None:
        from dataclasses import dataclass, field
        from agent_core.mcp.client import McpConnectionState

        registry = ToolRegistry()
        state = McpConnectionState(
            server_name="test",
            config=McpServerConfig(command="echo"),
            tools=[{"name": "t1", "description": "", "inputSchema": {}}],
        )
        register_mcp_tools(registry, "test", state)
        assert registry.contains("mcp__test__t1")

        # Re-register with fewer tools
        state.tools = [{"name": "t2", "description": "", "inputSchema": {}}]
        registered = register_mcp_tools(registry, "test", state)
        assert "mcp__test__t2" in registered
        assert not registry.contains("mcp__test__t1")  # stale removed


# ------------------------------------------------------------------
# Fatal error detection
# ------------------------------------------------------------------


class TestIsFatalError:
    def test_401_is_fatal(self) -> None:
        exc = Exception("unauthorized")
        exc.response = type("R", (), {"status_code": 401})()  # type: ignore[attr-defined]
        assert _is_fatal_error(exc) is True

    def test_403_is_fatal(self) -> None:
        exc = Exception("forbidden")
        exc.response = type("R", (), {"status_code": 403})()  # type: ignore[attr-defined]
        assert _is_fatal_error(exc) is True

    def test_500_is_not_fatal(self) -> None:
        exc = Exception("server error")
        exc.response = type("R", (), {"status_code": 500})()  # type: ignore[attr-defined]
        assert _is_fatal_error(exc) is False

    def test_502_is_not_fatal(self) -> None:
        exc = Exception("bad gateway")
        exc.response = type("R", (), {"status_code": 502})()  # type: ignore[attr-defined]
        assert _is_fatal_error(exc) is False

    def test_file_not_found_is_fatal(self) -> None:
        assert _is_fatal_error(FileNotFoundError("not found")) is True

    def test_not_implemented_is_fatal(self) -> None:
        assert _is_fatal_error(NotImplementedError("ws")) is True

    def test_value_error_is_fatal(self) -> None:
        assert _is_fatal_error(ValueError("invalid")) is True

    def test_timeout_is_not_fatal(self) -> None:
        assert _is_fatal_error(TimeoutError("timed out")) is False

    def test_auth_keyword_fallback(self) -> None:
        assert _is_fatal_error(Exception("invalid_grant")) is True
        assert _is_fatal_error(Exception("invalid_client")) is True
        # Generic "not found" must NOT be fatal
        assert _is_fatal_error(Exception("tool not found")) is False


# ------------------------------------------------------------------
# ToolRegistry query-scoped isolation
# ------------------------------------------------------------------


class TestToolRegistryIsolation:
    def test_separate_instances_are_isolated(self) -> None:
        from dataclasses import dataclass, field
        from agent_core.mcp.client import McpConnectionState

        r1 = ToolRegistry()
        r2 = ToolRegistry()

        state = McpConnectionState(
            server_name="srv",
            config=McpServerConfig(command="echo"),
            tools=[{"name": "t", "description": "", "inputSchema": {}}],
        )
        register_mcp_tools(r1, "srv", state)
        assert r1.contains("mcp__srv__t")
        assert not r2.contains("mcp__srv__t")
