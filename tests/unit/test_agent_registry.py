"""Unit tests for AgentRegistry — standalone and global default."""

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.registry import (
    AgentRegistry,
    _default_registry,
    get_agent,
    list_agents,
    register_agent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    """Fresh standalone registry (no builtins)."""
    return AgentRegistry()


def _def(agent_type: str, **kwargs) -> AgentDefinition:
    return AgentDefinition(agent_type=agent_type, **kwargs)


# ---------------------------------------------------------------------------
# Basic registration & query
# ---------------------------------------------------------------------------

class TestBasicRegistration:
    def test_register_and_get(self, registry):
        registry.register(_def("T"), source="builtin")
        assert registry.contains("T")
        assert registry.get("T").agent_type == "T"

    def test_register_unknown_source_raises(self, registry):
        with pytest.raises(ValueError, match="Unknown source"):
            registry.register(_def("T"), source="invalid")  # type: ignore[arg-type]

    def test_missing_agent_raises_keyerror(self, registry):
        with pytest.raises(KeyError, match="not registered"):
            registry.get("NonExistent")

    def test_contains_false(self, registry):
        assert not registry.contains("Nothing")

    def test_same_source_overwrites(self, registry):
        registry.register(_def("T", system_prompt="v1"), source="project")
        registry.register(_def("T", system_prompt="v2"), source="project")
        assert registry.get("T").system_prompt == "v2"


# ---------------------------------------------------------------------------
# Layer priority & merge
# ---------------------------------------------------------------------------

class TestLayerPriority:
    def test_scalar_override_low_to_high(self, registry):
        registry.register(_def("T", system_prompt="low"), source="builtin")
        registry.register(_def("T", system_prompt="high"), source="user")
        assert registry.get("T").system_prompt == "high"

    def test_disallowed_tools_union(self, registry):
        registry.register(_def("T", disallowed_tools=["A"]), source="builtin")
        registry.register(_def("T", disallowed_tools=["B"]), source="project")
        merged = registry.get("T")
        assert set(merged.disallowed_tools) == {"A", "B"}

    def test_auto_approve_tools_union(self, registry):
        registry.register(_def("T", auto_approve_tools=["Edit"]), source="builtin")
        registry.register(_def("T", auto_approve_tools=["Write"]), source="project")
        merged = registry.get("T")
        assert set(merged.auto_approve_tools) == {"Edit", "Write"}

    def test_mcp_servers_shallow_merge(self, registry):
        from agent_core.models import McpServerConfig

        srv_a = McpServerConfig(transport="stdio", command="a")  # type: ignore[call-arg]
        srv_b = McpServerConfig(transport="stdio", command="b")  # type: ignore[call-arg]

        registry.register(_def("T", mcp_servers={"a": srv_a}), source="builtin")
        registry.register(_def("T", mcp_servers={"b": srv_b}), source="project")
        merged = registry.get("T")
        assert "a" in merged.mcp_servers
        assert "b" in merged.mcp_servers

    def test_mcp_servers_same_key_override(self, registry):
        from agent_core.models import McpServerConfig

        srv_a = McpServerConfig(transport="stdio", command="old")  # type: ignore[call-arg]
        srv_b = McpServerConfig(transport="stdio", command="new")  # type: ignore[call-arg]

        registry.register(_def("T", mcp_servers={"x": srv_a}), source="builtin")
        registry.register(_def("T", mcp_servers={"x": srv_b}), source="user")
        merged = registry.get("T")
        assert merged.mcp_servers["x"].command == "new"

    def test_tools_override_by_higher_priority(self, registry):
        registry.register(_def("T", tools=["*"]), source="builtin")
        registry.register(_def("T", tools=["Read", "Glob"]), source="project")
        assert registry.get("T").tools == ["Read", "Glob"]

    def test_exclude_unset_prevents_default_override_of_tools(self, registry):
        """High layer without explicit tools must NOT overwrite low layer tools."""
        registry.register(_def("T", tools=["Read", "Glob"]), source="builtin")
        # project layer: no explicit tools → defaults to ["*"] but exclude_unset
        # means this default is NOT applied during merge
        registry.register(_def("T"), source="project")
        assert registry.get("T").tools == ["Read", "Glob"]

    def test_exclude_unset_prevents_default_override_of_system_prompt(self, registry):
        registry.register(_def("T", system_prompt="custom"), source="builtin")
        registry.register(_def("T"), source="user")
        assert registry.get("T").system_prompt == "custom"


# ---------------------------------------------------------------------------
# Unregister
# ---------------------------------------------------------------------------

class TestUnregister:
    def test_unregister_single_layer(self, registry):
        registry.register(_def("T", system_prompt="builtin"), source="builtin")
        registry.register(_def("T", system_prompt="project"), source="project")
        registry.unregister("T", source="project")
        assert registry.get("T").system_prompt == "builtin"

    def test_unregister_all_layers(self, registry):
        registry.register(_def("T"), source="builtin")
        registry.unregister("T")
        assert not registry.contains("T")

    def test_unregister_missing_noop(self, registry):
        registry.unregister("Nothing")  # should not raise


# ---------------------------------------------------------------------------
# List operations
# ---------------------------------------------------------------------------

class TestListOperations:
    def test_list_all(self, registry):
        registry.register(_def("A"), source="builtin")
        registry.register(_def("B"), source="builtin")
        all_agents = registry.list_all()
        assert set(all_agents) == {"A", "B"}

    def test_list_by_source(self, registry):
        registry.register(_def("A"), source="builtin")
        registry.register(_def("B"), source="project")
        builtin = registry.list_by_source("builtin")
        project = registry.list_by_source("project")
        assert set(builtin) == {"A"}
        assert set(project) == {"B"}

    def test_list_by_source_empty(self, registry):
        assert registry.list_by_source("plugin") == {}


# ---------------------------------------------------------------------------
# Global default registry convenience functions
# ---------------------------------------------------------------------------

class TestGlobalRegistry:
    def test_register_agent_direct_call(self):
        d = _def("DirectTest", system_prompt="direct")
        register_agent(d, source="project")
        try:
            assert get_agent("DirectTest").system_prompt == "direct"
        finally:
            _default_registry.unregister("DirectTest")

    def test_get_agent_keyerror(self):
        with pytest.raises(KeyError):
            get_agent("DefinitelyNotRegistered12345")

    def test_list_agents_includes_builtins(self):
        agents = list_agents()
        assert "GeneralPurpose" in agents
        assert "Explore" in agents
        assert "Plan" in agents
