"""Unit tests for built-in agent definitions."""

import pytest

from agent_core.agents.registry import _default_registry
from agent_core.permissions.engine import PermissionEngine


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------

class TestBuiltinRegistration:
    def test_all_three_registered(self):
        agents = _default_registry.list_all()
        assert "GeneralPurpose" in agents
        assert "Explore" in agents
        assert "Plan" in agents

    def test_agent_types_unique(self):
        agents = _default_registry.list_all()
        # agent_type is the key, so uniqueness is guaranteed by the dict.
        # Verify no duplicate in list_by_source across builtin layer.
        builtin = _default_registry.list_by_source("builtin")
        assert len(builtin) == 3


# ---------------------------------------------------------------------------
# GeneralPurpose
# ---------------------------------------------------------------------------

class TestGeneralPurpose:
    @pytest.fixture
    def gp(self):
        return _default_registry.get("GeneralPurpose")

    def test_tools_star(self, gp):
        assert gp.tools == ["*"]

    def test_permission_mode_none(self, gp):
        assert gp.permission_mode is None

    def test_auto_approve_tools_empty(self, gp):
        assert gp.auto_approve_tools == []

    def test_memory_none(self, gp):
        from agent_core.agents.definition import AgentMemory
        assert gp.memory == AgentMemory.NONE

    def test_has_when_to_use(self, gp):
        assert gp.when_to_use is not None
        assert len(gp.when_to_use) > 10


# ---------------------------------------------------------------------------
# Explore
# ---------------------------------------------------------------------------

class TestExplore:
    @pytest.fixture
    def explore(self):
        return _default_registry.get("Explore")

    def test_tools_read_only(self, explore):
        assert explore.tools == ["Read", "Glob", "Grep"]

    def test_permission_mode_none(self, explore):
        assert explore.permission_mode is None

    def test_auto_approve_tools_empty(self, explore):
        assert explore.auto_approve_tools == []

    def test_read_only_tools_approved_by_engine(self, explore):
        """Explore only has read-only tools → should auto-approve via engine step 6."""
        engine = PermissionEngine(mode="ask")  # strictest mode
        for tool_name in explore.tools:
            import asyncio
            decision = asyncio.run(
                engine.check(
                    tool_name=tool_name,
                    tool_input={},
                    callback=None,
                    tool_is_read_only=True,
                    tool_is_destructive=False,
                )
            )
            assert decision.allowed, (
                f"Tool '{tool_name}' should be allowed (engine step 6 "
                f"read-only fallback) but got: {decision.reason}"
            )

    def test_has_when_to_use(self, explore):
        assert explore.when_to_use is not None
        assert "searching across multiple files" in explore.when_to_use.lower()

    def test_has_system_prompt(self, explore):
        assert len(explore.system_prompt) > 20
        assert "read-only" in explore.system_prompt.lower()


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class TestPlan:
    @pytest.fixture
    def plan(self):
        return _default_registry.get("Plan")

    def test_tools_read_only(self, plan):
        assert plan.tools == ["Read", "Glob", "Grep"]

    def test_permission_mode_none(self, plan):
        assert plan.permission_mode is None

    def test_auto_approve_tools_empty(self, plan):
        assert plan.auto_approve_tools == []

    def test_has_when_to_use(self, plan):
        assert plan.when_to_use is not None
        assert "planning" in plan.when_to_use.lower() or "plan" in plan.when_to_use.lower()

    def test_has_system_prompt(self, plan):
        assert len(plan.system_prompt) > 20
        assert "plan" in plan.system_prompt.lower()
        assert "do not write" in plan.system_prompt.lower()
