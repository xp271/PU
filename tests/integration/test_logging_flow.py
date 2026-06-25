"""Integration tests for the conversation logging layer (Item 12).

Drives the real ``Agent.stream`` against a scripted ``MockClient`` with
logging enabled, then verifies the on-disk JSONL and the reader pipeline.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agent_core.agent import Agent
from agent_core.logging.config import LoggingConfig
from agent_core.logging.path import log_file_path
from agent_core.logging.reader import (
    aggregate_metrics,
    iter_conversations,
    load_log,
)
from agent_core.persistence.path import compute_project_hash
from tests.integration.mock_model import (
    MockClient,
    make_text_turn,
    make_tool_use_turn,
)


def _client_factory(client):
    def _factory(_model_id, _config=None):
        return client
    return _factory


async def _drive(agent: Agent, prompt: str) -> None:
    async for _ in agent.stream(prompt):
        pass
    await agent.aclose()


def _log_path(ws, log_dir, trace_id):
    return log_file_path(
        compute_project_hash(ws), None, log_dir=log_dir, trace_id=trace_id
    )


class TestLoggingFlow:
    def test_disabled_writes_nothing(self, tmp_path, monkeypatch):
        from agent_core.models import AgentConfig

        ws = str(tmp_path / "ws")
        log_dir = str(tmp_path / "logs")
        config = AgentConfig(
            max_turns=3,
            workspace_root=ws,
            permission_mode="bypassPermissions",
            logging=LoggingConfig(enabled=False, log_dir=log_dir),
        )
        client = MockClient(make_text_turn("hi"))

        async def _run():
            await _drive(Agent(config), "hello")

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            asyncio.run(_run())

        assert not os.path.isdir(log_dir) or not os.listdir(log_dir)

    def test_enabled_records_full_trace(self, tmp_path, monkeypatch):
        from agent_core.models import AgentConfig

        ws = str(tmp_path / "ws")
        log_dir = str(tmp_path / "logs")
        config = AgentConfig(
            max_turns=3,
            workspace_root=ws,
            permission_mode="bypassPermissions",
            logging=LoggingConfig(enabled=True, log_dir=log_dir),
        )
        # turn 1: tool use; turn 2: final text
        client = MockClient(make_text_turn("all done"))

        agent = Agent(config)

        async def _run():
            await _drive(agent, "please help")
            return agent._trace_id

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            trace_id = asyncio.run(_run())

        path = _log_path(ws, log_dir, trace_id)
        assert os.path.isfile(path)

        recs = asyncio.run(load_log(path))
        kinds = [r.kind for r in recs]
        assert kinds[0] == "session_start"
        assert "user_prompt" in kinds
        assert "assistant_text" in kinds
        assert "turn_metric" in kinds
        assert kinds[-1] == "session_end"

        # seq is monotonic
        seqs = [r.seq for r in recs]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        convos = iter_conversations(recs)
        assert len(convos) == 1
        assert convos[0].trace_id == trace_id
        assert convos[0].stop_reason == "end_turn"

        agg = aggregate_metrics(recs)
        assert agg["turns"] >= 1
        assert agg["total_tokens"] > 0

    def test_tool_use_recorded(self, tmp_path, monkeypatch):
        from agent_core.models import AgentConfig

        ws = str(tmp_path / "ws")
        os.makedirs(ws, exist_ok=True)
        log_dir = str(tmp_path / "logs")
        config = AgentConfig(
            max_turns=4,
            workspace_root=ws,
            permission_mode="bypassPermissions",
            logging=LoggingConfig(enabled=True, log_dir=log_dir),
        )

        # turn 1 -> Glob tool, turn 2 -> text
        class _ToolThenText(MockClient):
            def __init__(self):
                super().__init__([])
                self._call = 0

            async def stream_messages(self, **kwargs):
                from agent_core.llm.events import (
                    AssistantText,
                    StopEvent,
                    StreamStart,
                    TokenCounts,
                    ToolUse,
                )

                yield StreamStart(turn_index=kwargs.get("turn_index", 0), provider="mock", model=self.model)
                self._call += 1
                if self._call == 1:
                    yield ToolUse(tool_use_id="u1", tool_name="Glob", tool_input={"pattern": "*.py"})
                    yield TokenCounts(input_tokens=30, output_tokens=8)
                    yield StopEvent(stop_reason="tool_use")
                else:
                    yield AssistantText(content="found them")
                    yield TokenCounts(input_tokens=40, output_tokens=6)
                    yield StopEvent(stop_reason="end_turn")

        client = _ToolThenText()
        agent = Agent(config)

        async def _run():
            await _drive(agent, "list files")
            return agent._trace_id

        with monkeypatch.context() as m:
            m.setattr("agent_core.query.loop.get_client", _client_factory(client))
            trace_id = asyncio.run(_run())

        recs = asyncio.run(load_log(_log_path(ws, log_dir, trace_id)))
        kinds = [r.kind for r in recs]
        assert "tool_use" in kinds
        assert "tool_result" in kinds
        tu = [r for r in recs if r.kind == "tool_use"][0]
        assert tu.tool_name == "Glob"
        assert tu.tool_input == {"pattern": "*.py"}
