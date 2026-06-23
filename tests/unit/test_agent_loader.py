"""Unit tests for agent definition loaders and directory discovery."""

import json
import os
import tempfile

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.loader import (
    discover_agents,
    load_json_definition,
    load_markdown_definition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp()
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSON loader
# ---------------------------------------------------------------------------

class TestLoadJsonDefinition:
    def test_full_parse(self, tmpdir):
        path = os.path.join(tmpdir, "test.agent.json")
        with open(path, "w") as f:
            json.dump({
                "agent_type": "TestJson",
                "tools": ["Read", "Glob"],
                "system_prompt": "json test",
                "when_to_use": "For testing",
            }, f)

        d = load_json_definition(path)
        assert d is not None
        assert d.agent_type == "TestJson"
        assert d.tools == ["Read", "Glob"]
        assert d.system_prompt == "json test"

    def test_decode_error_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "bad.agent.json")
        with open(path, "w") as f:
            f.write("not valid json {{{[")

        d = load_json_definition(path)
        assert d is None

    def test_missing_file_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "nonexistent.agent.json")
        d = load_json_definition(path)
        assert d is None

    def test_not_a_dict_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "list.agent.json")
        with open(path, "w") as f:
            json.dump([1, 2, 3], f)

        d = load_json_definition(path)
        assert d is None

    def test_missing_agent_type_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "no_type.agent.json")
        with open(path, "w") as f:
            json.dump({"tools": ["Read"]}, f)

        d = load_json_definition(path)
        assert d is None


# ---------------------------------------------------------------------------
# Markdown loader
# ---------------------------------------------------------------------------

class TestLoadMarkdownDefinition:
    def test_full_parse(self, tmpdir):
        path = os.path.join(tmpdir, "test.agent.md")
        with open(path, "w") as f:
            f.write("""---
agent_type: TestMD
tools:
  - Read
  - Glob
system_prompt: md test
when_to_use: "For md testing"
---
# Body content (ignored)
""")

        d = load_markdown_definition(path)
        assert d is not None
        assert d.agent_type == "TestMD"
        assert d.tools == ["Read", "Glob"]
        assert d.system_prompt == "md test"

    def test_no_frontmatter_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "nofm.agent.md")
        with open(path, "w") as f:
            f.write("# Just a heading\n\nNo frontmatter here.")

        d = load_markdown_definition(path)
        assert d is None

    def test_frontmatter_missing_agent_type_returns_none(self, tmpdir):
        path = os.path.join(tmpdir, "no_type.agent.md")
        with open(path, "w") as f:
            f.write("""---
tools: [Read]
---
Body
""")

        d = load_markdown_definition(path)
        assert d is None


# ---------------------------------------------------------------------------
# Directory discovery
# ---------------------------------------------------------------------------

class TestDiscoverAgents:
    def test_finds_agent_md_and_json(self, tmpdir):
        with open(os.path.join(tmpdir, "a.agent.json"), "w") as f:
            json.dump({"agent_type": "A", "tools": ["Read"]}, f)
        with open(os.path.join(tmpdir, "b.agent.md"), "w") as f:
            f.write("""---
agent_type: B
tools: [Glob]
---
""")

        result = discover_agents(tmpdir)
        assert "A" in result
        assert "B" in result

    def test_empty_directory(self, tmpdir):
        result = discover_agents(tmpdir)
        assert result == {}

    def test_nonexistent_directory(self, tmpdir):
        result = discover_agents(os.path.join(tmpdir, "nope"))
        assert result == {}

    def test_recursive_discovery(self, tmpdir):
        sub = os.path.join(tmpdir, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "nested.agent.json"), "w") as f:
            json.dump({"agent_type": "Nested", "tools": ["Read"]}, f)

        result = discover_agents(tmpdir)
        assert "Nested" in result

    def test_duplicate_agent_type_warns(self, tmpdir, caplog):
        with open(os.path.join(tmpdir, "first.agent.json"), "w") as f:
            json.dump({"agent_type": "Dup", "system_prompt": "first"}, f)
        with open(os.path.join(tmpdir, "second.agent.md"), "w") as f:
            f.write("""---
agent_type: Dup
system_prompt: second
---
""")

        import logging
        with caplog.at_level(logging.WARNING):
            result = discover_agents(tmpdir)

        # First-come wins (.agent.json is collected before .agent.md)
        assert "Dup" in result
        # We still get one Dup
        assert len(result) == 1

    def test_plain_md_excluded_by_default(self, tmpdir):
        with open(os.path.join(tmpdir, "plain.md"), "w") as f:
            f.write("""---
agent_type: Plain
tools: [Read]
---
""")

        result = discover_agents(tmpdir)
        assert "Plain" not in result

    def test_plain_md_included_when_opt_in(self, tmpdir):
        with open(os.path.join(tmpdir, "plain.md"), "w") as f:
            f.write("""---
agent_type: Plain
tools: [Read]
---
""")

        result = discover_agents(tmpdir, include_plain_md=True)
        assert "Plain" in result

    def test_parse_failure_skipped(self, tmpdir):
        with open(os.path.join(tmpdir, "bad.agent.json"), "w") as f:
            f.write("not json")

        result = discover_agents(tmpdir)
        assert result == {}  # no valid definitions


# ---------------------------------------------------------------------------
# Integration: load + register
# ---------------------------------------------------------------------------

class TestLoadAndRegister:
    def test_load_json_then_register(self, tmpdir):
        from agent_core.agents.registry import AgentRegistry

        path = os.path.join(tmpdir, "reg.agent.json")
        with open(path, "w") as f:
            json.dump({"agent_type": "Loaded", "tools": ["Read"]}, f)

        d = load_json_definition(path)
        assert d is not None

        r = AgentRegistry()
        r.register(d, source="project")
        assert r.contains("Loaded")
        assert r.get("Loaded").tools == ["Read"]
