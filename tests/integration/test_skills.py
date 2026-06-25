"""
Integration tests for the skills system through the real ``query()`` loop.

A ``SKILL.md`` is discovered from ``config.skills_dir`` and executed via the
``Skill`` tool.  Because skill execution spawns a sub-agent through the same
patched client, we use a *scripted* mock that returns a different turn per
stream call (skill tool-use → child text → parent text) to keep execution
bounded.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_core.llm.events import InternalEvent, StreamStart
from agent_core.models import AgentConfig, AgentMessage
from agent_core.query.loop import query
from tests.integration.mock_model import MockClient, make_text_turn, make_tool_use_turn


class _ScriptedClient(MockClient):
    """Yields a different scripted turn for each ``stream_messages`` call.

    Calls beyond the script list repeat the final entry (a plain text turn),
    so any unexpected extra turn terminates cleanly.
    """

    def __init__(self, scripts: list[list[InternalEvent]]):
        super().__init__(script=[])
        self._scripts = scripts
        self._call = 0

    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        idx = self._call if self._call < len(self._scripts) else -1
        script = self._scripts[idx]
        self._call += 1
        yield StreamStart(turn_index=turn_index, provider="mock", model=self.model)
        for event in script:
            yield event


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


def _write_skill(skills_dir: Path, name: str, body: str, extra_fm: str = "") -> None:
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n{extra_fm}---\n{body}\n",
        encoding="utf-8",
    )


class TestSkillExecutionViaQuery:
    def test_inline_skill_runs_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)
            _write_skill(skills_dir, "greet", "Greet the user warmly.")

            scripts = [
                # parent turn 1: invoke the skill
                make_tool_use_turn("Skill", {"skill": "greet"}),
                # child turn 1 (sub-agent spawned by the skill): plain text
                make_text_turn("child greeting output"),
                # parent turn 2: wrap up
                make_text_turn("all done"),
            ]
            client = _ScriptedClient(scripts)
            config = AgentConfig(
                max_turns=4,
                permission_mode="bypassPermissions",
                skills_dir=str(skills_dir),
            )

            with patch(
                "agent_core.query.loop.get_client", _mock_factory(client)
            ):
                msgs = _collect(query("go", config, trace_id="s1"))

        tool_uses = [m for m in msgs if m.type == "tool_use"]
        assert any(m.tool_name == "Skill" for m in tool_uses)

        results = [m for m in msgs if m.type == "tool_result"]
        assert results, "expected a tool_result from the Skill tool"
        # The skill's sub-agent text is aggregated into the tool result.
        assert "child greeting output" in results[0].content
        assert not results[0].is_error

    def test_unknown_skill_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts = [
                make_tool_use_turn("Skill", {"skill": "does-not-exist"}),
                make_text_turn("done"),
            ]
            client = _ScriptedClient(scripts)
            config = AgentConfig(
                max_turns=3,
                permission_mode="bypassPermissions",
                skills_dir=str(tmp),
            )

            with patch(
                "agent_core.query.loop.get_client", _mock_factory(client)
            ):
                msgs = _collect(query("go", config, trace_id="s2"))

        results = [m for m in msgs if m.type == "tool_result"]
        assert results and results[0].is_error
        assert "Unknown skill" in results[0].content
