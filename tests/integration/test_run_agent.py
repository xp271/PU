"""Integration tests for the sub-agent execution engine (plan §8.5).

Exercise ``run_agent()`` / ``AgentTool`` against controlled MockClient
scripts — no real API keys.  Covers: basic flow, tool-pool filtering,
permission downgrade, sidechain output, custom system injection,
sidechain↔resume roundtrip, recursion-depth gate, and the async
background path (launch → drain → ``<task-notification>`` delivery).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.run import run_agent
from agent_core.agents.tool import AgentInput, AgentTool, get_bg_registry
from agent_core.models import AgentConfig
from agent_core.tools.base import ToolUseContext
from tests.integration.mock_model import (
    MockClient,
    make_text_turn,
    make_tool_use_turn,
)
from agent_core.llm.events import StopEvent, StreamStart, TokenCounts, ToolUse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect(agen) -> list:
    async def _drain():
        out = []
        async for msg in agen:
            out.append(msg)
        return out

    return asyncio.run(_drain())


def _factory(client: MockClient):
    def f(_model_id, _config):
        return client

    return f


def _dispatch_factory(default_client, **by_substr):
    """Return get_client dispatching on the model id string."""

    def f(model_id, _config):
        s = str(model_id)
        for substr, client in by_substr.items():
            if substr in s:
                return client
        return default_client

    return f


class CapturingClient(MockClient):
    """MockClient that records the ``system`` it is invoked with."""

    def __init__(self, script, sink: list[str | None], model: str = "mock-model"):
        super().__init__(script, model=model)
        self._sink = sink

    async def stream_messages(self, messages, tools, system=None, max_tokens=4096, turn_index=0):
        self._sink.append(system)
        async for ev in super().stream_messages(
            messages, tools, system=system, max_tokens=max_tokens, turn_index=turn_index
        ):
            yield ev


class ScriptedClient(MockClient):
    """MockClient yielding a different script per turn (clamps to the last)."""

    def __init__(self, scripts: list[list], model: str = "mock-model"):
        super().__init__(scripts[0], model=model)
        self._scripts = scripts
        self._turn = 0

    async def stream_messages(self, messages, tools, system=None, max_tokens=4096, turn_index=0):
        idx = min(self._turn, len(self._scripts) - 1)
        script = self._scripts[idx]
        self._turn += 1
        yield StreamStart(turn_index=turn_index, provider="mock", model=self.model)
        for ev in script:
            yield ev


def _agent_tooluse_turn(subagent_type: str, run_in_background: bool = True):
    return [
        ToolUse(
            tool_use_id="tu_agent_1",
            tool_name="Agent",
            tool_input={
                "description": "bg task",
                "prompt": "do background work",
                "subagent_type": subagent_type,
                "run_in_background": run_in_background,
            },
        ),
        TokenCounts(input_tokens=10, output_tokens=5),
        StopEvent(stop_reason="tool_use"),
    ]


@pytest.fixture
def sidechain_tmp(tmp_path, monkeypatch):
    """Redirect sub-agent sidechain output into a temp directory."""
    d = tmp_path / "sidechain"
    d.mkdir()
    monkeypatch.setattr(
        "agent_core.agents.run._sidechain_dir", lambda _trace: str(d)
    )
    return d


# ---------------------------------------------------------------------------
# 1. Basic flow
# ---------------------------------------------------------------------------


def test_run_agent_basic_flow(sidechain_tmp):
    client = MockClient(make_text_turn("child done"))
    definition = AgentDefinition(agent_type="t", tools=["Read"])
    config = AgentConfig(max_turns=5, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        msgs = _collect(
            run_agent(
                "do x",
                definition,
                parent_config=config,
                parent_trace_id="t1",
                parent_allowed_tools=frozenset({"Read"}),
            )
        )

    types = [m.type for m in msgs]
    assert "assistant_text" in types
    assert "done" in types
    assert any(m.type == "assistant_text" and "child done" in m.content for m in msgs)


# ---------------------------------------------------------------------------
# 2. Tool-pool filtering
# ---------------------------------------------------------------------------


def test_tool_pool_filtering_blocks_unallowed(sidechain_tmp):
    # Child only allowed Read; parent allows Read+Bash. Child tries Bash.
    client = MockClient(make_tool_use_turn("Bash", {"command": "ls"}))
    definition = AgentDefinition(agent_type="t", tools=["Read"])
    config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        msgs = _collect(
            run_agent(
                "x",
                definition,
                parent_config=config,
                parent_trace_id="t2",
                parent_allowed_tools=frozenset({"Read", "Bash"}),
            )
        )

    tool_results = [m for m in msgs if m.type == "tool_result"]
    assert tool_results, "expected a tool_result"
    assert any(
        tr.is_error and "Unknown tool" in str(tr.content) for tr in tool_results
    ), "Bash should be unavailable to the child"


# ---------------------------------------------------------------------------
# 3. Permission downgrade (parent bypass → child ask → callback consulted)
# ---------------------------------------------------------------------------


def test_permission_downgrade_invokes_parent_callback(sidechain_tmp, tmp_path):
    calls: list[str] = []

    async def parent_cb(tool_name: str, tool_input: dict) -> bool:
        calls.append(tool_name)
        return False  # deny

    client = MockClient(
        make_tool_use_turn(
            "Write", {"file_path": str(tmp_path / "x.txt"), "content": "hi"}
        )
    )
    definition = AgentDefinition(
        agent_type="t", tools=["Write"], permission_mode="ask"
    )
    # Parent is fully permissive; child declares ask → must clamp to ask.
    config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        msgs = _collect(
            run_agent(
                "x",
                definition,
                parent_config=config,
                parent_trace_id="t3",
                parent_permission_callback=parent_cb,
                parent_allowed_tools=frozenset({"Write"}),
            )
        )

    assert "Write" in calls, "clamped child must consult the parent ask callback"
    tool_results = [m for m in msgs if m.type == "tool_result"]
    assert any(
        tr.is_error and "Permission denied" in str(tr.content) for tr in tool_results
    )
    assert not (tmp_path / "x.txt").exists(), "denied write must not touch disk"


# ---------------------------------------------------------------------------
# 4. Sidechain JSONL output
# ---------------------------------------------------------------------------


def test_sidechain_jsonl_written(sidechain_tmp):
    client = MockClient(make_tool_use_turn("Bash", {"command": "ls"}))
    definition = AgentDefinition(agent_type="t", tools=["Read"])
    config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        _collect(
            run_agent(
                "x",
                definition,
                parent_config=config,
                parent_trace_id="t4",
                parent_allowed_tools=frozenset({"Read"}),
            )
        )

    files = list(Path(sidechain_tmp).glob("*.jsonl"))
    assert files, "a sidechain file should be created"
    lines = [
        json.loads(ln)
        for ln in files[0].read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert lines, "sidechain should contain at least one record"
    for rec in lines:
        assert rec["role"] in ("user", "assistant")
        assert isinstance(rec["content"], list)


# ---------------------------------------------------------------------------
# 5. Custom system-prompt injection (§1.5 query(system=))
# ---------------------------------------------------------------------------


def test_custom_system_prompt_injected(sidechain_tmp):
    captured: list[str | None] = []
    client = CapturingClient(make_text_turn("ok"), captured)
    definition = AgentDefinition(
        agent_type="t",
        tools=["Read"],
        system_prompt="You are a code exploration specialist.",
    )
    config = AgentConfig(max_turns=3, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        _collect(
            run_agent(
                "x",
                definition,
                parent_config=config,
                parent_trace_id="t5",
                parent_allowed_tools=frozenset({"Read"}),
            )
        )

    assert captured, "client should have been invoked"
    assert any(
        s and "code exploration specialist" in s for s in captured
    ), "child system prompt must reach the LLM call"


# ---------------------------------------------------------------------------
# 6. Sidechain ↔ resume roundtrip
# ---------------------------------------------------------------------------


def test_resume_from_sidechain(sidechain_tmp, tmp_path):
    from agent_core.agents.fork import resume_agent

    # Write a transcript in the §3.3 format.
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "earlier work"}],
                "turn": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    client = MockClient(make_text_turn("resumed and finished"))
    definition = AgentDefinition(agent_type="t", tools=["Read"])
    config = AgentConfig(max_turns=3, permission_mode="bypassPermissions")

    with patch("agent_core.query.loop.get_client", _factory(client)):
        msgs = _collect(
            resume_agent(
                "a1",
                str(transcript),
                definition,
                parent_config=config,
                parent_trace_id="t6",
                parent_allowed_tools=frozenset({"Read"}),
            )
        )

    types = [m.type for m in msgs]
    assert "done" in types
    assert any(
        m.type == "assistant_text" and "resumed" in m.content for m in msgs
    )


# ---------------------------------------------------------------------------
# 7. Recursion-depth gate removes the Agent tool at the limit
# ---------------------------------------------------------------------------


def test_recursion_depth_removes_agent_tool(sidechain_tmp):
    from agent_core.agents.resolve import MAX_AGENT_DEPTH

    client = MockClient(
        make_tool_use_turn("Agent", {"description": "d", "prompt": "p"})
    )
    definition = AgentDefinition(agent_type="t", tools=["*"])
    config = AgentConfig(max_turns=2, permission_mode="bypassPermissions")

    # Parent context already at the depth limit → child (depth+1) loses Agent.
    parent_ctx = ToolUseContext(agent_depth=MAX_AGENT_DEPTH)

    with patch("agent_core.query.loop.get_client", _factory(client)):
        msgs = _collect(
            run_agent(
                "x",
                definition,
                parent_config=config,
                parent_trace_id="t7",
                parent_allowed_tools=frozenset({"Read", "Agent"}),
                parent_context=parent_ctx,
            )
        )

    tool_results = [m for m in msgs if m.type == "tool_result"]
    assert any(
        tr.is_error and "Unknown tool" in str(tr.content) for tr in tool_results
    ), "Agent tool must be unavailable past the depth limit"


# ---------------------------------------------------------------------------
# 8. Async background: launch → drain → notification delivery
# ---------------------------------------------------------------------------


def test_async_launch_drain_and_notification(sidechain_tmp):
    """AgentTool background launch is tracked, drained, and yields a
    <task-notification> into the parent notification queue."""
    definition = AgentDefinition(agent_type="bgworker", tools=["Read"])
    config = AgentConfig(max_turns=3, permission_mode="bypassPermissions")
    notif_queue: list[str] = []

    parent_ctx = ToolUseContext(
        agent_id="parent",
        parent_config=config,
        parent_trace_id="t8",
        parent_allowed_tools=frozenset({"Read"}),
        notification_queue=notif_queue,
    )

    child_client = MockClient(make_text_turn("background result"))
    tool = AgentTool()
    inp = AgentInput(
        description="d",
        prompt="p",
        subagent_type="bgworker",
        run_in_background=True,
    )

    async def _run():
        with patch("agent_core.query.loop.get_client", _factory(child_client)), patch(
            "agent_core.agents.tool.get_agent", lambda _t: definition
        ):
            result = await tool.call(inp, parent_ctx)
            # Returned immediately with an agent_id
            payload = json.loads(result.content)
            assert payload["status"] == "async_launched"
            # Drain the tracked background task (what query() does at exit)
            reg = get_bg_registry(parent_ctx)
            assert reg is not None
            await reg.drain()
            return payload["agent_id"]

    agent_id = asyncio.run(_run())

    assert notif_queue, "drain must deliver a <task-notification>"
    note = notif_queue[0]
    assert "<task-notification>" in note
    assert agent_id in note
    assert "<status>completed</status>" in note


def test_async_path_runs_to_completion_via_query(sidechain_tmp):
    """End-to-end: parent query() spawns a background sub-agent, then drains
    it at the terminal turn so the child actually completes (sidechain written)."""
    definition = AgentDefinition(
        agent_type="bgworker2", tools=["Read"], model="anthropic/claude-haiku-4-5"
    )

    parent_client = ScriptedClient(
        [
            _agent_tooluse_turn("bgworker2", run_in_background=True),
            make_text_turn("parent done"),
        ]
    )
    child_client = MockClient(make_text_turn("child bg done"))

    config = AgentConfig(max_turns=5, permission_mode="bypassPermissions")

    from agent_core.query.loop import query

    with patch(
        "agent_core.query.loop.get_client",
        _dispatch_factory(parent_client, haiku=child_client),
    ), patch("agent_core.agents.tool.get_agent", lambda _t: definition):
        msgs = _collect(query("go", config, trace_id="t9"))

    # Parent received the async-launch acknowledgement.
    assert any(
        m.type == "tool_result" and "async_launched" in str(m.content) for m in msgs
    )
    assert any(m.type == "done" for m in msgs)
    # The background child ran to completion (drain awaited it) → sidechain file.
    files = list(Path(sidechain_tmp).glob("*.jsonl"))
    assert files, "background child should have written a sidechain transcript"
    content = files[0].read_text(encoding="utf-8")
    assert "child bg done" in content
