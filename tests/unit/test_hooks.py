"""
Unit tests for the hooks system (Item 9):
- Type models (HookBlock, HookModify, HookContinue, StopResult)
- HookRegistry (registration, routing, priority, isolation)
- Pipeline executors (pre/post/stop/subagent-start)
- Integration with run_tool_use (in query/loop.py)
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from agent_core.hooks.types import (
    HookBlock,
    HookContinue,
    HookKind,
    HookModify,
    HookResult,
    PreToolUseHook,
    PostToolUseHook,
    StopHook,
    StopResult,
    SubagentStartHook,
)
from agent_core.hooks.registry import HookRegistry
from agent_core.hooks.pipeline import (
    run_pre_tool_use_hooks,
    run_post_tool_use_hooks,
    run_stop_hooks,
    run_subagent_start_hooks,
)
from agent_core.tools.base import ToolCallResult, ToolUseContext


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def ctx() -> ToolUseContext:
    return ToolUseContext(agent_id="test-agent", workspace_root="/tmp/test")


@pytest.fixture
def registry() -> HookRegistry:
    return HookRegistry()


# ============================================================================
# Type models
# ============================================================================


class TestHookBlock:
    def test_defaults(self):
        b = HookBlock()
        assert b.action == "block"
        assert b.reason == ""
        assert b.message is None

    def test_with_reason(self):
        b = HookBlock(reason="not allowed", message="try something else")
        assert b.reason == "not allowed"
        assert b.message == "try something else"


class TestHookModify:
    def test_replaces_content(self):
        m = HookModify(new_result="changed")
        assert m.action == "modify"
        assert m.new_result == "changed"


class TestHookContinue:
    def test_is_continue(self):
        c = HookContinue()
        assert c.action == "continue"


class TestStopResult:
    def test_defaults_to_continue(self):
        r = StopResult()
        assert r.should_stop is False

    def test_stop_with_reason(self):
        r = StopResult(should_stop=True, reason="goal achieved", message="Done.")
        assert r.should_stop is True
        assert r.reason == "goal achieved"
        assert r.message == "Done."


# ============================================================================
# HookRegistry
# ============================================================================


class TestRegistryRegistration:
    def test_register_pre_tool_use(self, registry):
        h = _make_hook("pre_tool_use")
        registry.register(h, priority=10)
        assert registry.pre_tool_use_hooks() == [h]

    def test_register_post_tool_use(self, registry):
        h = _make_hook("post_tool_use")
        registry.register(h)
        assert registry.post_tool_use_hooks() == [h]

    def test_register_stop(self, registry):
        h = _make_stop_hook(should_stop=False)
        registry.register(h, priority=5)
        assert registry.stop_hooks() == [h]

    def test_register_subagent_start(self, registry):
        h = _make_hook("subagent_start")
        registry.register(h)
        assert registry.subagent_start_hooks() == [h]

    def test_missing_hook_kind_raises(self, registry):
        with pytest.raises(ValueError, match="missing a valid hook_kind"):
            registry.register(object())

    def test_register_all(self, registry):
        hooks = [_make_hook("pre_tool_use"), _make_hook("post_tool_use")]
        registry.register_all(hooks)
        assert len(registry.pre_tool_use_hooks()) == 1
        assert len(registry.post_tool_use_hooks()) == 1

    def test_is_empty(self, registry):
        assert registry.is_empty() is True
        registry.register(_make_hook("pre_tool_use"))
        assert registry.is_empty() is False


class TestRegistryPriority:
    def test_sorted_by_priority(self, registry):
        h1 = _make_hook("pre_tool_use")
        h2 = _make_hook("pre_tool_use")
        h3 = _make_hook("pre_tool_use")
        registry.register(h1, priority=50)
        registry.register(h2, priority=10)
        registry.register(h3, priority=30)
        assert registry.pre_tool_use_hooks() == [h2, h3, h1]

    def test_same_priority_stable_order(self, registry):
        """Same-priority hooks keep registration order (monotonic seq)."""
        hooks = [_make_hook("pre_tool_use") for _ in range(5)]
        for h in hooks:
            registry.register(h, priority=100)
        assert registry.pre_tool_use_hooks() == hooks


class TestRegistryCopyWithOverrides:
    def test_copy_is_independent(self, registry):
        h1 = _make_hook("pre_tool_use")
        registry.register(h1)
        child = registry.copy_with_overrides([])
        # Child has parent hooks
        assert len(child.pre_tool_use_hooks()) == 1
        # Mutating child does not affect parent
        child.register(_make_hook("stop"))
        assert registry.is_empty() is False  # parent still has h1
        assert len(child.stop_hooks()) == 1
        # Parent still has only 1 hook (child mutation is isolated)

    def test_extra_hooks_appended(self, registry):
        registry.register(_make_hook("pre_tool_use"))
        extra = _make_hook("post_tool_use")
        child = registry.copy_with_overrides([extra])
        assert len(child.pre_tool_use_hooks()) == 1
        assert len(child.post_tool_use_hooks()) == 1

    def test_empty_parent_copy(self):
        r = HookRegistry()
        extra = _make_hook("pre_tool_use")
        child = r.copy_with_overrides([extra])
        assert child.pre_tool_use_hooks() == [extra]


class TestRegistryBucketIsolation:
    """Different hook kinds do not interleave."""

    def test_kinds_in_separate_buckets(self, registry):
        pre = _make_hook("pre_tool_use")
        post = _make_hook("post_tool_use")
        stop = _make_stop_hook(True)
        sub = _make_hook("subagent_start")
        registry.register_all([pre, post, stop, sub])
        assert registry.pre_tool_use_hooks() == [pre]
        assert registry.post_tool_use_hooks() == [post]
        assert registry.stop_hooks() == [stop]
        assert registry.subagent_start_hooks() == [sub]


# ============================================================================
# Pipeline executors
# ============================================================================


class TestPreToolUsePipeline:
    @pytest.mark.asyncio
    async def test_continue_passes(self, registry, ctx):
        h = _tracking_hook("pre_tool_use", HookContinue())
        registry.register(h)
        mod, reason = await run_pre_tool_use_hooks("Read", {"path": "/f"}, ctx, registry)
        assert mod is None
        assert reason is None
        assert h.called  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_block_returns_reason(self, registry, ctx):
        h = _tracking_hook("pre_tool_use", HookBlock(reason="danger"))
        registry.register(h)
        mod, reason = await run_pre_tool_use_hooks("Bash", {"cmd": "rm"}, ctx, registry)
        assert mod is None
        assert reason == "danger"

    @pytest.mark.asyncio
    async def test_modify_returns_new_input(self, registry, ctx):
        new_input = {"cmd": "ls"}
        h = _tracking_hook("pre_tool_use", HookModify(new_result=new_input))
        registry.register(h)
        mod, reason = await run_pre_tool_use_hooks("Bash", {"cmd": "rm"}, ctx, registry)
        assert mod == new_input
        assert reason is None

    @pytest.mark.asyncio
    async def test_first_block_wins(self, registry, ctx):
        h1 = _tracking_hook("pre_tool_use", HookBlock(reason="first"))
        h2 = _tracking_hook("pre_tool_use", HookBlock(reason="second"))
        registry.register(h1, priority=10)
        registry.register(h2, priority=20)
        mod, reason = await run_pre_tool_use_hooks("Read", {}, ctx, registry)
        assert reason == "first"
        # h2 never called (short-circuit)
        assert not h2.called  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_modify_chain(self, registry, ctx):
        h1 = _tracking_hook("pre_tool_use", HookModify(new_result="first"))
        h2 = _tracking_hook("pre_tool_use", HookModify(new_result="second"))
        registry.register(h1, priority=10)
        registry.register(h2, priority=20)
        mod, reason = await run_pre_tool_use_hooks("Write", {}, ctx, registry)
        # Last modification wins
        assert mod == "second"
        assert reason is None

    @pytest.mark.asyncio
    async def test_none_registry_short_circuits(self, ctx):
        mod, reason = await run_pre_tool_use_hooks("Read", {}, ctx, None)
        assert mod is None
        assert reason is None

    @pytest.mark.asyncio
    async def test_empty_registry_short_circuits(self, registry, ctx):
        mod, reason = await run_pre_tool_use_hooks("Read", {}, ctx, registry)
        assert mod is None
        assert reason is None


class TestPostToolUsePipeline:
    @pytest.mark.asyncio
    async def test_continue_passes_result(self, registry, ctx):
        h = _tracking_hook("post_tool_use", HookContinue())
        registry.register(h)
        original = ToolCallResult(content="ok")
        result = await run_post_tool_use_hooks("Read", {}, original, ctx, registry)
        assert result.content == "ok"
        assert not result.is_error
        assert h.called  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_block_marks_error(self, registry, ctx):
        h = _tracking_hook("post_tool_use", HookBlock(reason="unsafe output"))
        registry.register(h)
        original = ToolCallResult(content="secret")
        result = await run_post_tool_use_hooks("Read", {}, original, ctx, registry)
        assert result.is_error is True
        assert "unsafe output" in result.content

    @pytest.mark.asyncio
    async def test_modify_replaces_content(self, registry, ctx):
        h = _tracking_hook("post_tool_use", HookModify(new_result="sanitized"))
        registry.register(h)
        original = ToolCallResult(content="raw")
        result = await run_post_tool_use_hooks("Read", {}, original, ctx, registry)
        assert result.content == "sanitized"

    @pytest.mark.asyncio
    async def test_modify_chain_last_wins(self, registry, ctx):
        h1 = _tracking_hook("post_tool_use", HookModify(new_result="v1"))
        h2 = _tracking_hook("post_tool_use", HookModify(new_result="v2"))
        registry.register(h1, priority=10)
        registry.register(h2, priority=20)
        original = ToolCallResult(content="orig")
        result = await run_post_tool_use_hooks("Write", {}, original, ctx, registry)
        assert result.content == "v2"


class TestStopPipeline:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_no_stop(self, registry, ctx):
        result = await run_stop_hooks([], ctx, registry)
        assert result.should_stop is False

    @pytest.mark.asyncio
    async def test_hook_returns_stop(self, registry, ctx):
        h = _tracking_stop_hook(True, "done")
        registry.register(h)
        result = await run_stop_hooks([], ctx, registry)
        assert result.should_stop is True

    @pytest.mark.asyncio
    async def test_first_stop_wins(self, registry, ctx):
        h1 = _tracking_stop_hook(True, "first", priority=10)
        h2 = _tracking_stop_hook(True, "second", priority=20)
        registry.register(h1, priority=10)
        registry.register(h2, priority=20)
        result = await run_stop_hooks([], ctx, registry)
        assert result.should_stop is True
        assert result.reason == "first"


class TestSubagentStartPipeline:
    @pytest.mark.asyncio
    async def test_no_hooks_allows(self, registry, ctx):
        blocked, reason = await run_subagent_start_hooks(
            "Explore", "search", None, ctx, registry
        )
        assert blocked is False

    @pytest.mark.asyncio
    async def test_block_denies(self, registry, ctx):
        h = _tracking_hook("subagent_start", HookBlock(reason="rate limit"))
        registry.register(h)
        blocked, reason = await run_subagent_start_hooks(
            "Explore", "search", None, ctx, registry
        )
        assert blocked is True
        assert reason == "rate limit"

    @pytest.mark.asyncio
    async def test_modify_ignored(self, registry, ctx):
        """HookModify is treated as continue for SubagentStart (no input to modify)."""
        h = _tracking_hook("subagent_start", HookModify(new_result="ignored"))
        registry.register(h)
        blocked, reason = await run_subagent_start_hooks(
            "Explore", "search", None, ctx, registry
        )
        assert blocked is False


# ============================================================================
# Helpers
# ============================================================================


def _make_hook(kind: HookKind):
    """Create a minimal concrete hook instance with the given hook_kind."""

    class _Hook:
        hook_kind: ClassVar[HookKind] = kind

        def __init__(self):
            self.result: HookResult = HookContinue()

        async def __call__(self, *args: Any, **kwargs: Any) -> HookResult:
            return self.result

    return _Hook()


def _make_stop_hook(should_stop: bool, reason: str = ""):
    class _Hook:
        hook_kind: ClassVar[HookKind] = "stop"

        def __init__(self):
            self.result = StopResult(should_stop=should_stop, reason=reason)

        async def __call__(self, *args: Any, **kwargs: Any) -> StopResult:
            return self.result

    return _Hook()


def _tracking_hook(kind: HookKind, result: HookResult):
    """Hook that records whether it was called."""

    class _Tracking:
        hook_kind: ClassVar[HookKind] = kind

        def __init__(self):
            self.result = result
            self.called = False

        async def __call__(self, *args: Any, **kwargs: Any) -> HookResult:
            self.called = True
            return self.result

    return _Tracking()


def _tracking_stop_hook(
    should_stop: bool, reason: str = "", priority: int = 100
):
    class _Tracking:
        hook_kind: ClassVar[HookKind] = "stop"

        def __init__(self):
            self.result = StopResult(should_stop=should_stop, reason=reason)
            self.called = False

        async def __call__(self, *args: Any, **kwargs: Any) -> StopResult:
            self.called = True
            return self.result

    return _Tracking()
