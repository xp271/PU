"""
Unit tests for the skills system (Item 9):
- SkillDefinition model
- SkillRegistry CRUD
- Skill loader (SKILL.md parsing)
- Template rendering
- SkillPermissionEngine
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_core.skills.definition import SkillDefinition, SkillParameter
from agent_core.skills.registry import SkillRegistry
from agent_core.skills.loader import load_skill_from_markdown, discover_skills
from agent_core.skills.executor import render_template
from agent_core.skills.permissions import (
    SkillPermissionEngine,
    SkillPermissionRule,
    default_skill_permissions,
)


# ============================================================================
# SkillDefinition model
# ============================================================================


class TestSkillDefinition:
    def test_minimal_definition(self):
        sd = SkillDefinition(name="test-skill")
        assert sd.name == "test-skill"
        assert sd.tools == ["*"]
        assert sd.execution_mode == "inline"

    def test_full_definition(self):
        sd = SkillDefinition(
            name="review",
            description="Review code",
            tools=["Read", "Grep"],
            model="anthropic/claude-haiku-4-5",
            execution_mode="fork",
            background=True,
            parameters={
                "pr": SkillParameter(type="string", required=True),
            },
        )
        assert sd.tools == ["Read", "Grep"]
        assert sd.model == "anthropic/claude-haiku-4-5"
        assert sd.execution_mode == "fork"
        assert sd.parameters["pr"].required is True


# ============================================================================
# SkillRegistry
# ============================================================================


class TestSkillRegistry:
    def test_register_and_get(self):
        reg = SkillRegistry()
        sd = SkillDefinition(name="test-skill")
        reg.register(sd)
        assert reg.get("test-skill") is sd

    def test_get_unknown_raises(self):
        reg = SkillRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_contains(self):
        reg = SkillRegistry()
        assert not reg.contains("x")
        reg.register(SkillDefinition(name="x"))
        assert reg.contains("x")

    def test_list_all(self):
        reg = SkillRegistry()
        reg.register(SkillDefinition(name="a"))
        reg.register(SkillDefinition(name="b"))
        assert set(reg.list_all().keys()) == {"a", "b"}

    def test_overwrite_on_register(self):
        reg = SkillRegistry()
        sd1 = SkillDefinition(name="s", description="first")
        sd2 = SkillDefinition(name="s", description="second")
        reg.register(sd1)
        reg.register(sd2)
        assert reg.get("s").description == "second"

    def test_unregister(self):
        reg = SkillRegistry()
        reg.register(SkillDefinition(name="s"))
        reg.unregister("s")
        assert not reg.contains("s")


# ============================================================================
# SkillLoader
# ============================================================================


class TestLoadSkillFromMarkdown:
    def test_parses_basic_frontmatter(self):
        content = """---
name: test-skill
description: A test skill
tools:
  - Read
  - Grep
execution_mode: fork
---
Do something with {{ input }}.
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            sd = load_skill_from_markdown(f.name)
        Path(f.name).unlink()

        assert sd is not None
        assert sd.name == "test-skill"
        assert sd.description == "A test skill"
        assert sd.tools == ["Read", "Grep"]
        assert sd.execution_mode == "fork"
        assert "{{ input }}" in sd.prompt

    def test_returns_none_for_missing_name(self):
        content = """---
description: No name field
---
Body.
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            sd = load_skill_from_markdown(f.name)
        Path(f.name).unlink()
        assert sd is None

    def test_returns_none_for_non_existent_file(self):
        assert load_skill_from_markdown("/nonexistent/file.md") is None

    def test_parses_parameters(self):
        content = """---
name: param-skill
parameters:
  input_file:
    type: string
    required: true
  verbose:
    type: string
    required: false
---
Read {{ input_file }}.
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            sd = load_skill_from_markdown(f.name)
        Path(f.name).unlink()

        assert sd is not None
        assert "input_file" in sd.parameters
        assert sd.parameters["input_file"].required is True
        assert sd.parameters["verbose"].required is False


class TestDiscoverSkills:
    def test_discovers_skill_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a skill directory with SKILL.md
            skill_dir = Path(tmp) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: A skill\n---\nBody.\n"
            )
            # Also create a non-skill directory (no SKILL.md)
            (Path(tmp) / "other-dir").mkdir()

            skills = discover_skills(tmp)
            assert len(skills) == 1
            assert "my-skill" in skills

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = discover_skills(tmp)
            assert skills == {}

    def test_nonexistent_directory(self):
        skills = discover_skills("/nonexistent/dir")
        assert skills == {}


# ============================================================================
# Template rendering
# ============================================================================


class TestRenderTemplate:
    def test_basic_substitution(self):
        result = render_template("Hello {{ name }}!", {"name": "World"}, {})
        assert result == "Hello World!"

    def test_spaces_inside_braces(self):
        result = render_template("{{   name    }}", {"name": "ok"}, {})
        assert result == "ok"

    def test_missing_optional_preserved(self):
        result = render_template("{{ x }} {{ y }}", {"x": "1"}, {})
        assert result == "1 {{ y }}"

    def test_missing_required_raises(self):
        params = {"x": SkillParameter(required=True)}
        with pytest.raises(ValueError, match="Missing required"):
            render_template("{{ x }}", {}, params)

    def test_multiple_placeholders(self):
        result = render_template(
            "{{ a }} and {{ b }}", {"a": "1", "b": "2"}, {}
        )
        assert result == "1 and 2"


# ============================================================================
# SkillPermissionEngine
# ============================================================================


class TestSkillPermissionEngine:
    def test_default_allow_star(self):
        engine = default_skill_permissions()
        assert engine.check("any-skill") == "ask"

    def test_exact_match_first(self):
        engine = SkillPermissionEngine(
            rules=[
                SkillPermissionRule(pattern="dangerous-skill", action="deny", priority=10),
                SkillPermissionRule(pattern="*", action="allow", priority=100),
            ]
        )
        assert engine.check("dangerous-skill") == "deny"
        assert engine.check("safe-skill") == "allow"

    def test_fnmatch_prefix(self):
        engine = SkillPermissionEngine(
            rules=[
                SkillPermissionRule(pattern="review:*", action="allow", priority=10),
                SkillPermissionRule(pattern="*", action="ask", priority=100),
            ]
        )
        assert engine.check("review:code") == "allow"
        assert engine.check("review:pr") == "allow"
        assert engine.check("deploy") == "ask"

    def test_priority_ordering(self):
        engine = SkillPermissionEngine(
            rules=[
                SkillPermissionRule(pattern="*", action="allow", priority=100),
                SkillPermissionRule(pattern="dangerous-*", action="deny", priority=10),
            ]
        )
        # Lower priority wins (10 < 100)
        assert engine.check("dangerous-rm") == "deny"
        assert engine.check("safe-one") == "allow"
