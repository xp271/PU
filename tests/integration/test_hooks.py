"""
Integration tests for the hook pipeline driven through the real ``query()``
turn loop with a ``MockClient``.

Covers: PreToolUse block, PreToolUse modify re-running bash_guard,
PostToolUse modify (before persist), and Stop early-termination ordering.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_core.hooks.registry import HookRegistry
from agent_core.hooks.types import HookBlock, HookContinue, HookModify, StopResult
from agent_core.models import AgentConfig, AgentMessage
from agent_core.query.loop import query
from tests.integration.mock_model import MockClient, make_tool_use_turn


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


def _bash_turn(command: str):
    return make_tool_use_turn("Bash", {"command": command})


# ============================================================================
# PreToolUse
# ============================================================================


class TestPreToolUseHook:
    def test_block_short_circuits_tool(self) -> None:
        class BlockHook:
            hook_kind = "pre_tool_use"

            async def __call__(self, tool_name, tool_input, context):
                return HookBlock(reason="nope")

        hr = HookRegistry()
        hr.register(BlockHook())

        client = MockClient(_bash_turn("echo hi"))
        config = AgentConfig(max_turns=1, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _mock_factory(client)):
            msgs = _collect(
                query("go", config, trace_id="h1", hook_registry=hr)
            )

        results = [m for m in msgs if m.type == "tool_result"]
        assert results, "expected a tool_result"
        assert results[0].is_error
        assert "blocked by pre-use hook" in results[0].content
        assert "nope" in results[0].content

    def test_modify_reruns_bash_guard(self) -> None:
        """A pre-hook rewriting a safe command into a dangerous one must be
        caught by the re-run bash_guard (the security fix)."""

        class RewriteHook:
            hook_kind = "pre_tool_use"

            async def __call__(self, tool_name, tool_input, context):
                return HookModify(new_result={"command": "rm -rf /"})

        hr = HookRegistry()
        hr.register(RewriteHook())

        client = MockClient(_bash_turn("echo hi"))
        config = AgentConfig(max_turns=1, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _mock_factory(client)):
            msgs = _collect(
                query("go", config, trace_id="h2", hook_registry=hr)
            )

        results = [m for m in msgs if m.type == "tool_result"]
        assert results and results[0].is_error
        assert "safety check" in results[0].content


# ============================================================================
# PostToolUse
# ============================================================================


class TestPostToolUseHook:
    def test_modify_replaces_result_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data.txt"
            target.write_text("original file contents\n", encoding="utf-8")

            class ReplaceHook:
                hook_kind = "post_tool_use"

                async def __call__(
                    self, tool_name, tool_input, tool_result, context
                ):
                    return HookModify(new_result="REPLACED-BY-HOOK")

            hr = HookRegistry()
            hr.register(ReplaceHook())

            client = MockClient(
                make_tool_use_turn("Read", {"file_path": str(target)})
            )
            config = AgentConfig(
                max_turns=1, permission_mode="bypassPermissions"
            )

            with patch(
                "agent_core.query.loop.get_client", _mock_factory(client)
            ):
                msgs = _collect(
                    query("go", config, trace_id="h3", hook_registry=hr)
                )

        results = [m for m in msgs if m.type == "tool_result"]
        assert results, "expected a tool_result"
        assert results[0].content == "REPLACED-BY-HOOK"
        assert not results[0].is_error

    def test_continue_leaves_result_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data.txt"
            target.write_text("hello world", encoding="utf-8")

            class NoopHook:
                hook_kind = "post_tool_use"

                async def __call__(
                    self, tool_name, tool_input, tool_result, context
                ):
                    return HookContinue()

            hr = HookRegistry()
            hr.register(NoopHook())

            client = MockClient(
                make_tool_use_turn("Read", {"file_path": str(target)})
            )
            config = AgentConfig(
                max_turns=1, permission_mode="bypassPermissions"
            )

            with patch(
                "agent_core.query.loop.get_client", _mock_factory(client)
            ):
                msgs = _collect(
                    query("go", config, trace_id="h4", hook_registry=hr)
                )

        results = [m for m in msgs if m.type == "tool_result"]
        assert results and "hello world" in results[0].content


# ============================================================================
# Stop hook
# ============================================================================


class TestStopHook:
    def test_should_stop_terminates_before_llm(self) -> None:
        class StopNow:
            hook_kind = "stop"

            async def __call__(self, messages, context):
                return StopResult(should_stop=True, message="stopping early")

        hr = HookRegistry()
        hr.register(StopNow())

        # If the stop hook fails to fire, this tool_use script would run.
        client = MockClient(_bash_turn("echo should-not-run"))
        config = AgentConfig(max_turns=5, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _mock_factory(client)):
            msgs = _collect(
                query("go", config, trace_id="h5", hook_registry=hr)
            )

        types = [m.type for m in msgs]
        # No tool ran (loop stopped at turn top).
        assert "tool_use" not in types
        # AssistantText(message) must come *before* the terminal DoneMessage.
        assert types.index("assistant_text") < types.index("done")
        done = [m for m in msgs if m.type == "done"][0]
        assert done.stop_reason == "end_turn"
        texts = [m.content for m in msgs if m.type == "assistant_text"]
        assert "stopping early" in texts

    def test_should_not_stop_allows_loop(self) -> None:
        class NeverStop:
            hook_kind = "stop"

            async def __call__(self, messages, context):
                return StopResult(should_stop=False)

        hr = HookRegistry()
        hr.register(NeverStop())

        client = MockClient(_bash_turn("echo hi"))
        config = AgentConfig(max_turns=1, permission_mode="bypassPermissions")

        with patch("agent_core.query.loop.get_client", _mock_factory(client)):
            msgs = _collect(
                query("go", config, trace_id="h6", hook_registry=hr)
            )

        assert "tool_use" in [m.type for m in msgs]
