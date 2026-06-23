"""Unit tests for AgentDefinition model and AgentMemory enum."""

import pytest

from agent_core.agents.definition import AgentDefinition, AgentMemory


# ---------------------------------------------------------------------------
# AgentMemory
# ---------------------------------------------------------------------------

class TestAgentMemory:
    def test_enum_values(self):
        assert AgentMemory.USER == "user"
        assert AgentMemory.PROJECT == "project"
        assert AgentMemory.LOCAL == "local"
        assert AgentMemory.NONE == "none"
        assert len(AgentMemory) == 4

    def test_enum_is_str(self):
        assert isinstance(AgentMemory.NONE, str)


# ---------------------------------------------------------------------------
# AgentDefinition construction & defaults
# ---------------------------------------------------------------------------

class TestAgentDefinitionDefaults:
    def test_minimal_construction(self):
        d = AgentDefinition(agent_type="Test")
        assert d.agent_type == "Test"
        assert d.when_to_use is None
        assert d.tools == ["*"]
        assert d.disallowed_tools == []
        assert d.auto_approve_tools == []
        assert d.system_prompt == ""
        assert d.model is None
        assert d.permission_mode is None
        assert d.mcp_servers == {}
        assert d.hooks == []
        assert d.skills == []
        assert d.memory == AgentMemory.NONE
        assert d.background is False

    def test_full_construction(self):
        d = AgentDefinition(
            agent_type="FullAgent",
            when_to_use="For testing",
            tools=["Read", "Glob"],
            disallowed_tools=["Bash"],
            auto_approve_tools=["Edit"],
            system_prompt="system test",
            model="anthropic/claude-haiku-4-5",
            permission_mode="ask",
            mcp_servers={},
            hooks=[],
            skills=["my-skill"],
            memory=AgentMemory.LOCAL,
            background=True,
        )
        assert d.agent_type == "FullAgent"
        assert d.tools == ["Read", "Glob"]
        assert d.disallowed_tools == ["Bash"]
        assert d.auto_approve_tools == ["Edit"]
        assert d.system_prompt == "system test"
        assert d.model == "anthropic/claude-haiku-4-5"
        assert d.permission_mode == "ask"
        assert d.skills == ["my-skill"]
        assert d.memory == AgentMemory.LOCAL
        assert d.background is True

    def test_permission_mode_none(self):
        d = AgentDefinition(agent_type="T")
        assert d.permission_mode is None

    def test_permission_mode_values(self):
        for mode in ("bypassPermissions", "acceptEdits", "ask"):
            d = AgentDefinition(agent_type="T", permission_mode=mode)
            assert d.permission_mode == mode


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestAgentDefinitionValidation:
    def test_empty_agent_type_rejected(self):
        with pytest.raises(ValueError, match="agent_type must not be empty"):
            AgentDefinition(agent_type="")

    def test_whitespace_only_agent_type_rejected(self):
        with pytest.raises(ValueError):
            AgentDefinition(agent_type="   ")

    def test_invalid_chars_rejected(self):
        with pytest.raises(ValueError, match="contains invalid characters"):
            AgentDefinition(agent_type="bad name!")

    def test_special_chars_each_rejected(self):
        for ch in ("@", "/", " ", "!"):
            with pytest.raises(ValueError):
                AgentDefinition(agent_type=f"bad{ch}name")

    def test_valid_chars_accepted(self):
        for name in ("MyAgent", "my-agent", "my_agent", "Agent123", "a"):
            d = AgentDefinition(agent_type=name)
            assert d.agent_type == name

    def test_empty_tools_rejected(self):
        with pytest.raises(ValueError, match="tools must not be empty"):
            AgentDefinition(agent_type="T", tools=[])


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestAgentDefinitionSerialization:
    def test_roundtrip(self):
        original = AgentDefinition(
            agent_type="RT",
            when_to_use="roundtrip test",
            tools=["Read", "Glob"],
            disallowed_tools=["Bash"],
            auto_approve_tools=["Write"],
            system_prompt="rt prompt",
            model="anthropic/claude-opus-4-7",
            permission_mode="acceptEdits",
        )
        data = original.model_dump()
        restored = AgentDefinition.model_validate(data)
        assert restored == original

    def test_model_dump_exclude_unset_minimal(self):
        d = AgentDefinition(agent_type="Min")
        dumped = d.model_dump(exclude_unset=True)
        assert set(dumped.keys()) == {"agent_type"}

    def test_model_dump_exclude_unset_with_tools(self):
        d = AgentDefinition(agent_type="T", tools=["Read"])
        dumped = d.model_dump(exclude_unset=True)
        assert "agent_type" in dumped
        assert "tools" in dumped
        # tools=["*"] is the default; our explicit ["Read"] is NOT the default
        assert dumped["tools"] == ["Read"]

    def test_model_dump_exclude_unset_tools_star(self):
        """Explicit tools=['*'] should appear in exclude_unset (same value, different source)."""
        d = AgentDefinition(agent_type="T", tools=["*"])
        dumped = d.model_dump(exclude_unset=True)
        # Pydantic exclude_unset: if the field was explicitly set (even to
        # its default), it is included.  This is the desired behaviour for
        # the merge algorithm.
        assert "tools" in dumped
        assert dumped["tools"] == ["*"]
