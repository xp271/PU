"""Unit tests for agents/resolve.py — Item 7.6 consumption contract."""

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.resolve import (
    MAX_AGENT_DEPTH,
    build_child_engine,
    build_child_system_prompt,
    disallow_agent_at_depth_limit,
    merge_mcp_servers,
    resolve_agent_tools,
    resolve_model,
)
from agent_core.models import (
    McpServerConfig,
    ModelId,
    ModelProvider,
    PermissionMode,
)
from agent_core.permissions.inheritance import (
    compute_child_allowed_tools,
    compute_child_disallowed_tools,
    compute_child_permission_mode,
)
from agent_core.permissions.rules import PermissionRule


# ---------------------------------------------------------------------------
# resolve_agent_tools
# ---------------------------------------------------------------------------


class TestResolveAgentTools:
    def test_wildcard_expands_to_parent_all(self):
        d = AgentDefinition(agent_type="test", tools=["*"])
        parent_allowed = frozenset(["Read", "Glob", "Grep", "Bash"])
        parent_disallowed = frozenset()
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert allowed == parent_allowed
        assert disallowed == frozenset()

    def test_intersection_narrows_tools(self):
        d = AgentDefinition(agent_type="test", tools=["Read", "Bash"])
        parent_allowed = frozenset(["Read", "Glob"])
        parent_disallowed = frozenset()
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert allowed == frozenset(["Read"])
        assert "Bash" not in allowed

    def test_disallowed_union_and_dedup(self):
        d = AgentDefinition(
            agent_type="test",
            tools=["*"],
            disallowed_tools=["Write"],
        )
        parent_allowed = frozenset(["Read", "Write", "Bash"])
        parent_disallowed = frozenset(["Bash"])
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert "Bash" in disallowed
        assert "Write" in disallowed
        assert "Bash" not in allowed
        assert "Write" not in allowed
        assert "Read" in allowed

    def test_empty_intersection(self):
        d = AgentDefinition(agent_type="test", tools=["Bash"])
        parent_allowed = frozenset(["Read"])
        parent_disallowed = frozenset()
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert allowed == frozenset()

    def test_parent_disallowed_takes_precedence(self):
        """A tool explicitly disallowed by parent is removed from allowed."""
        d = AgentDefinition(agent_type="test", tools=["Read", "Bash"])
        parent_allowed = frozenset(["Read", "Bash"])
        parent_disallowed = frozenset(["Bash"])
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert "Bash" not in allowed
        assert "Read" in allowed


# ---------------------------------------------------------------------------
# build_child_engine
# ---------------------------------------------------------------------------


class TestBuildChildEngine:
    def test_auto_approve_compiles_allow_rule(self):
        d = AgentDefinition(
            agent_type="test",
            auto_approve_tools=["Edit"],
        )
        engine = build_child_engine(d, "ask", frozenset())
        # The engine should have an allow rule for "Edit"
        # Verify by checking that Edit with ask mode passes
        import asyncio
        decision = asyncio.run(
            engine.check("Edit", {}, None, tool_is_read_only=False, tool_is_destructive=True)
        )
        assert decision.allowed is True
        assert decision.source == "rule"

    def test_parent_disallowed_excluded_from_auto_approve(self):
        d = AgentDefinition(
            agent_type="test",
            auto_approve_tools=["Bash"],
        )
        engine = build_child_engine(d, "ask", frozenset(["Bash"]))
        import asyncio
        # Bash should NOT be auto-approved; with ask mode and no callback → denied (destructive)
        decision = asyncio.run(
            engine.check("Bash", {}, None, tool_is_read_only=False, tool_is_destructive=True)
        )
        assert decision.source != "rule"

    def test_mode_clamped_to_less_permissive(self):
        d = AgentDefinition(agent_type="test", permission_mode="ask")
        engine = build_child_engine(d, "bypassPermissions", frozenset())
        assert engine.mode == "ask"

    def test_mode_inherited_when_none(self):
        d = AgentDefinition(agent_type="test", permission_mode=None)
        engine = build_child_engine(d, "ask", frozenset())
        assert engine.mode == "ask"

    def test_mode_inherited_accept_edits(self):
        d = AgentDefinition(agent_type="test", permission_mode=None)
        engine = build_child_engine(d, "acceptEdits", frozenset())
        assert engine.mode == "acceptEdits"


# ---------------------------------------------------------------------------
# merge_mcp_servers
# ---------------------------------------------------------------------------


class TestMergeMcpServers:
    def test_child_overrides_same_key(self):
        parent = {"filesystem": McpServerConfig(command="cmd1")}
        child = AgentDefinition(
            agent_type="test",
            mcp_servers={"filesystem": McpServerConfig(command="cmd2")},
        )
        merged = merge_mcp_servers(child, parent)
        assert merged["filesystem"].command == "cmd2"

    def test_different_keys_merged(self):
        parent = {"a": McpServerConfig(command="a")}
        child = AgentDefinition(
            agent_type="test",
            mcp_servers={"b": McpServerConfig(command="b")},
        )
        merged = merge_mcp_servers(child, parent)
        assert set(merged) == {"a", "b"}

    def test_empty_child_preserves_parent(self):
        parent = {"a": McpServerConfig(command="a")}
        child = AgentDefinition(agent_type="test")
        merged = merge_mcp_servers(child, parent)
        assert merged == parent

    def test_empty_parent_uses_child(self):
        child = AgentDefinition(
            agent_type="test",
            mcp_servers={"a": McpServerConfig(command="a")},
        )
        merged = merge_mcp_servers(child, {})
        assert merged == {"a": McpServerConfig(command="a")}

    def test_does_not_mutate_input(self):
        parent = {"x": McpServerConfig(command="x")}
        child = AgentDefinition(agent_type="test")
        merge_mcp_servers(child, parent)
        # parent dict unchanged
        assert "x" in parent


# ---------------------------------------------------------------------------
# build_child_system_prompt
# ---------------------------------------------------------------------------


class TestBuildChildSystemPrompt:
    def test_parts_in_correct_order(self):
        d = AgentDefinition(
            agent_type="test",
            system_prompt="I am a child agent.",
        )
        result = build_child_system_prompt(
            d,
            parent_system="I am the parent.",
            tool_names=frozenset(["Read", "Glob"]),
        )
        # Child prompt first, then tools, then parent
        assert result.index("I am a child agent.") < result.index("Available tools")
        assert result.index("Available tools") < result.index("I am the parent.")

    def test_empty_child_prompt_omitted(self):
        d = AgentDefinition(agent_type="test")
        result = build_child_system_prompt(d, "parent", frozenset(["Read"]))
        assert "Available tools" in result
        assert "parent" in result

    def test_empty_tools_omitted(self):
        d = AgentDefinition(agent_type="test", system_prompt="child")
        result = build_child_system_prompt(d, "parent", frozenset())
        assert "Available tools" not in result
        assert "child" in result
        assert "parent" in result

    def test_empty_parent_omitted(self):
        d = AgentDefinition(agent_type="test", system_prompt="child")
        result = build_child_system_prompt(d, "", frozenset(["Read"]))
        assert result == "child\n\nAvailable tools: Read"


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_inherits_when_none(self):
        d = AgentDefinition(agent_type="test")
        parent = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        result = resolve_model(d, parent)
        assert result.provider == ModelProvider.ANTHROPIC
        assert result.name == "claude-sonnet-4-5"

    def test_overrides_when_set(self):
        d = AgentDefinition(agent_type="test", model="anthropic/claude-haiku-4-5")
        parent = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        result = resolve_model(d, parent)
        assert result.name == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# disallow_agent_at_depth_limit
# ---------------------------------------------------------------------------


class TestDisallowAgentAtDepthLimit:
    def test_at_limit_removes_agent(self):
        allowed = frozenset(["Read", "Agent", "Glob"])
        result = disallow_agent_at_depth_limit(allowed, MAX_AGENT_DEPTH)
        assert "Agent" not in result
        assert "Read" in result

    def test_below_limit_keeps_agent(self):
        allowed = frozenset(["Read", "Agent"])
        result = disallow_agent_at_depth_limit(allowed, MAX_AGENT_DEPTH - 1)
        assert "Agent" in result

    def test_above_limit_removes_agent(self):
        allowed = frozenset(["Read", "Agent"])
        result = disallow_agent_at_depth_limit(allowed, MAX_AGENT_DEPTH + 1)
        assert "Agent" not in result


# ---------------------------------------------------------------------------
# Inheritance invariants (cross-cutting)
# ---------------------------------------------------------------------------


class TestInheritanceInvariants:
    """Security invariants defined in Item 7 plan, verified at resolution."""

    def test_child_cannot_elevate_permission(self):
        """Child mode must never exceed parent mode."""
        # This is tested indirectly via build_child_engine mode clamping.
        d = AgentDefinition(agent_type="test", permission_mode="bypassPermissions")
        engine = build_child_engine(d, "ask", frozenset())
        assert engine.mode == "ask"  # clamped down

    def test_child_cannot_use_parent_forbidden(self):
        """Parent disallowed tools must remain disallowed for child."""
        d = AgentDefinition(agent_type="test", tools=["*"])
        parent_allowed = frozenset(["Read", "Bash"])
        parent_disallowed = frozenset(["Bash"])
        allowed, disallowed = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert "Bash" not in allowed
        assert "Bash" in disallowed

    def test_child_only_uses_mutual_allowed(self):
        """Child tools must be subset of parent_allowed ∩ child_tools."""
        d = AgentDefinition(agent_type="test", tools=["Read", "Write"])
        parent_allowed = frozenset(["Read", "Glob"])
        parent_disallowed = frozenset()
        allowed, _ = resolve_agent_tools(d, parent_allowed, parent_disallowed)
        assert allowed.issubset(parent_allowed)
        assert allowed.issubset(frozenset(d.tools))
