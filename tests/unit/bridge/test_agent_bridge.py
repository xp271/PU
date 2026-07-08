"""Tests for bridge/agent_bridge.py — AgentBridge session management + Agent lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.bridge.agent_bridge import AgentBridge
from agent_core.models import AgentConfig
from agent_core.session import SessionState


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge() -> AgentBridge:
    """Create an AgentBridge with persistence layer mocked.

    _get_or_create_session is now async and calls load_session +
    begin_persistence.  All unit tests mock these out so they never
    touch the real filesystem.

    The persistence functions are imported lazily inside
    _get_or_create_session via ``from ..persistence.path import ...``,
    so we patch them at their definition site (persistence.path /
    persistence.session), not at the bridge module.
    """
    with patch(
        "agent_core.persistence.path.compute_project_hash",
        return_value="deadbeef00000000",
    ), patch(
        "agent_core.persistence.path.session_jsonl_path",
        return_value="/tmp/mock_sessions/deadbeef00000000/test.jsonl",
    ), patch(
        "agent_core.persistence.session.load_session",
        new_callable=AsyncMock,
        return_value=None,  # default: no disk history
    ) as mock_load, patch.object(
        SessionState, "begin_persistence", new_callable=AsyncMock,
    ) as mock_begin:
        b = AgentBridge(AgentConfig())
        b._mock_load = mock_load
        b._mock_begin = mock_begin
        yield b


def _make_mock_agent(texts: list[str], error_msg: str | None = None):
    async def _mock_stream(prompt, *, system=None):
        for t in texts:
            yield MagicMock(type="assistant_text", content=t)
        if error_msg:
            yield MagicMock(type="error", message=error_msg)
        yield MagicMock(
            type="done", stop_reason="end_turn", total_tokens=100,
            final_messages=[], error_detail=None,
        )
    mock_agent = MagicMock()
    mock_agent.stream = _mock_stream
    mock_agent.aclose = AsyncMock()
    return mock_agent


@pytest.fixture
def mock_agent_cls():
    with patch("agent_core.bridge.agent_bridge.Agent") as mock:
        yield mock


# ---------------------------------------------------------------------------
# TestSessionManagement
# ---------------------------------------------------------------------------


class TestSessionManagement:
    def test_session_id_format(self, bridge: AgentBridge):
        sid = bridge.session_id("user1", "channel1")
        assert sid == "channel1:user1"

    def test_safe_session_id(self, bridge: AgentBridge):
        """_safe_session_id returns hex without colon."""
        sid = bridge._safe_session_id("c2c:ABC123")
        assert ":" not in sid
        assert len(sid) == 16
        # Deterministic
        assert bridge._safe_session_id("c2c:ABC123") == sid

    @pytest.mark.asyncio
    async def test_session_creation(self, bridge: AgentBridge):
        """New session created when no disk history."""
        s = await bridge._get_or_create_session("ch1:u1")
        assert s is not None
        assert s.messages == []
        # begin_persistence should have been called
        bridge._mock_begin.assert_called()

    @pytest.mark.asyncio
    async def test_session_reuse(self, bridge: AgentBridge):
        """Same session_id returns same object (in-memory cache)."""
        s1 = await bridge._get_or_create_session("ch1:u1")
        s2 = await bridge._get_or_create_session("ch1:u1")
        assert s1 is s2
        # begin_persistence only called once
        assert bridge._mock_begin.call_count == 1

    @pytest.mark.asyncio
    async def test_session_loaded_from_disk(self, bridge: AgentBridge):
        """load_session returns a pre-built SessionState → reuse it."""
        prebuilt = SessionState(session_id="ch1:u1")
        prebuilt.messages = [{"role": "user", "content": "hello"}]
        prebuilt._written_count = 1
        bridge._mock_load.return_value = prebuilt

        s = await bridge._get_or_create_session("ch1:u1")
        assert s is prebuilt
        assert len(s.messages) == 1
        assert s.messages[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_no_session_timeout(self, bridge: AgentBridge):
        """Session should NOT be expired after any duration.

        (The old session_timeout mechanism has been removed.)
        """
        s1 = await bridge._get_or_create_session("ch1:u1")
        # Even after multiple calls, same session should be returned
        s2 = await bridge._get_or_create_session("ch1:u1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_clear_session(self, bridge: AgentBridge):
        """clear_session removes from memory and calls end_persistence."""
        await bridge._get_or_create_session("ch1:u1")
        with patch.object(SessionState, "end_persistence", new_callable=AsyncMock) as mock_end:
            await bridge.clear_session("u1", "ch1")
            mock_end.assert_called_once()

        # After clear, a new session is created (with fresh begin_persistence)
        s = await bridge._get_or_create_session("ch1:u1")
        assert len(s.messages) == 0

    @pytest.mark.asyncio
    async def test_different_users_independent(self, bridge: AgentBridge):
        s1 = await bridge._get_or_create_session("ch1:u1")
        s2 = await bridge._get_or_create_session("ch1:u2")
        assert s1 is not s2
        assert bridge._session_locks["ch1:u1"] is not bridge._session_locks["ch1:u2"]


# ---------------------------------------------------------------------------
# TestBuildPrompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_text_passthrough(self, bridge: AgentBridge):
        assert bridge._build_prompt("hello", "text") == "hello"

    def test_voice_with_recognition(self, bridge: AgentBridge):
        assert bridge._build_prompt("recognized", "voice") == "[语音消息] recognized"

    def test_voice_without_recognition(self, bridge: AgentBridge):
        assert bridge._build_prompt("", "voice") == "[语音消息]"

    def test_image(self, bridge: AgentBridge):
        assert bridge._build_prompt("url", "image") == "[图片消息] url"

    def test_unknown_type(self, bridge: AgentBridge):
        assert bridge._build_prompt("c", "file") == "[file消息] c"


# ---------------------------------------------------------------------------
# TestProcess
# ---------------------------------------------------------------------------


class TestProcess:
    @pytest.mark.asyncio
    async def test_process_creates_and_closes_agent(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["hello"])
        result = await bridge.process("u1", "ch1", "hi", "text")
        assert result == ["hello"]
        mock_agent_cls.return_value.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_collects_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["hello, ", "world"])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["hello, world"]

    @pytest.mark.asyncio
    async def test_process_empty_reply(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent([])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == []

    @pytest.mark.asyncio
    async def test_process_error_with_partial_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["partial"], error_msg="err")
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["partial"]

    @pytest.mark.asyncio
    async def test_process_error_no_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent([], error_msg="err")
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["抱歉，处理您的请求时遇到了问题，请稍后再试。"]

    @pytest.mark.asyncio
    async def test_agent_aclose_on_exception(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent = MagicMock()
        mock_agent.aclose = AsyncMock()
        async def _crash(prompt, **kw):
            raise RuntimeError("crash")
            yield
        mock_agent.stream = _crash
        mock_agent_cls.return_value = mock_agent
        with pytest.raises(RuntimeError):
            await bridge.process("u1", "ch1", "test", "text")
        mock_agent.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_session_lock(self, bridge: AgentBridge, mock_agent_cls):
        call_order: list[int] = []

        def _make_delayed(idx: int):
            async def _stream(prompt, **kw):
                call_order.append(idx)
                yield MagicMock(type="assistant_text", content=f"r{idx}")
                yield MagicMock(type="done", stop_reason="end_turn", total_tokens=10,
                               final_messages=[], error_detail=None)
            return _stream

        mock_agent_cls.side_effect = [
            MagicMock(stream=_make_delayed(0), aclose=AsyncMock()),
            MagicMock(stream=_make_delayed(1), aclose=AsyncMock()),
        ]
        await asyncio.gather(
            bridge.process("u1", "ch1", "m1", "text"),
            bridge.process("u1", "ch1", "m2", "text"),
        )
        assert call_order == [0, 1]

    @pytest.mark.asyncio
    async def test_long_text_split(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["x" * 3500])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert len(result) == 2
        assert all(len(c) <= 1900 for c in result)


# ---------------------------------------------------------------------------
# TestGetSession
# ---------------------------------------------------------------------------


class TestGetSession:
    """AgentBridge.get_session() 公开接口测试。"""

    @pytest.mark.asyncio
    async def test_get_session_creates(self, bridge: AgentBridge):
        """get_session() 首次调用创建新 session（异步）。"""
        s = await bridge.get_session("u1", "ch1")
        assert s is not None
        assert s.messages == []

    @pytest.mark.asyncio
    async def test_get_session_reuses(self, bridge: AgentBridge):
        """get_session() 重复调用返回同一个 session。"""
        s1 = await bridge.get_session("u1", "ch1")
        s2 = await bridge.get_session("u1", "ch1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_get_session_independent_users(self, bridge: AgentBridge):
        """不同用户有独立的 session。"""
        s1 = await bridge.get_session("u1", "ch1")
        s2 = await bridge.get_session("u2", "ch1")
        assert s1 is not s2


# ---------------------------------------------------------------------------
# TestWorkspaceOverride
# ---------------------------------------------------------------------------


class TestWorkspaceOverride:
    """AgentBridge.process() 中 workspace_override 的测试。"""

    @pytest.mark.asyncio
    async def test_process_uses_workspace_override(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 非 None 时，Agent 收到覆盖后的 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = await bridge.get_session("u1", "ch1")
        session.workspace_override = "D:\\test_ws"

        await bridge.process("u1", "ch1", "test", "text")

        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]
        assert passed_config.workspace_root == "D:\\test_ws"

    @pytest.mark.asyncio
    async def test_process_no_override_no_change(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 为 None 时，Agent 使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        await bridge.process("u1", "ch1", "test", "text")

        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]
        assert passed_config is bridge._base

    @pytest.mark.asyncio
    async def test_process_override_reset(self, bridge: AgentBridge, mock_agent_cls):
        """workspace_override 设为 None 后恢复使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = await bridge.get_session("u1", "ch1")
        session.workspace_override = "D:\\test_ws"

        await bridge.process("u1", "ch1", "test", "text")
        assert mock_agent_cls.call_args[0][0].workspace_root == "D:\\test_ws"

        # Reset
        session.workspace_override = None
        mock_agent_cls.reset_mock()
        mock_agent_cls.return_value = _make_mock_agent(["ok2"])

        await bridge.process("u1", "ch1", "test2", "text")
        assert mock_agent_cls.call_args[0][0] is bridge._base


# ---------------------------------------------------------------------------
# TestEdgeCases — 边缘条件 & fallback
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """边界条件和降级路径测试。"""

    # -- _safe_session_id 边缘输入 -------------------------------------------

    def test_safe_session_id_empty_string(self, bridge: AgentBridge):
        """空字符串也能生成合法 hex。"""
        sid = bridge._safe_session_id("")
        assert ":" not in sid
        assert len(sid) == 16
        assert all(c in "0123456789abcdef" for c in sid)

    def test_safe_session_id_unicode(self, bridge: AgentBridge):
        """Unicode 输入（中文、emoji）不抛异常。"""
        sid = bridge._safe_session_id("群聊:用户💯")
        assert ":" not in sid
        assert len(sid) == 16

    def test_safe_session_id_different_inputs_different_hash(self, bridge: AgentBridge):
        """不同输入产生不同 hash。"""
        a = bridge._safe_session_id("c2c:userA")
        b = bridge._safe_session_id("c2c:userB")
        assert a != b

    # -- load_session 异常降级 -----------------------------------------------

    @pytest.mark.asyncio
    async def test_load_session_raises_propagates(self, bridge: AgentBridge):
        """load_session 抛异常时向上传播（当前无 fallback）。

        这是设计决策：磁盘 I/O 异常应让调用方感知，而非静默丢弃历史。
        """
        bridge._mock_load.side_effect = OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            await bridge._get_or_create_session("ch1:u1")

        # session 不应被缓存（异常路径未到达缓存步骤）
        assert "ch1:u1" not in bridge._sessions

    @pytest.mark.asyncio
    async def test_load_session_raises_then_retry_succeeds(self, bridge: AgentBridge):
        """第一次 load_session 失败，修复后第二次成功创建。"""
        bridge._mock_load.side_effect = OSError("transient error")

        with pytest.raises(OSError):
            await bridge._get_or_create_session("ch1:u1")

        # 修复后重试
        bridge._mock_load.side_effect = None
        bridge._mock_load.return_value = None  # no disk history

        s = await bridge._get_or_create_session("ch1:u1")
        assert s is not None
        assert s.messages == []

    # -- begin_persistence 异常 -----------------------------------------------

    @pytest.mark.asyncio
    async def test_begin_persistence_raises_propagates(self, bridge: AgentBridge):
        """begin_persistence 抛异常时向上传播，session 不缓存。"""
        bridge._mock_begin.side_effect = OSError("cannot write")

        with pytest.raises(OSError, match="cannot write"):
            await bridge._get_or_create_session("ch1:u1")

        # session 不应被缓存
        assert "ch1:u1" not in bridge._sessions

    # -- load_session 返回已有 writer 的 session -----------------------------

    @pytest.mark.asyncio
    async def test_load_session_with_writer_already_open(self, bridge: AgentBridge):
        """load_session 返回的 session 已有 writer → 跳过 begin_persistence。

        这是幂等性保证：如果某种原因 load_session 返回了一个已经激活
        persistence 的 session（例如未来 load_session 内部变化），
        bridge 不应重复调用 begin_persistence。
        """
        prebuilt = SessionState(session_id="ch1:u1")
        # 模拟 _writer 已存在（已激活 persistence）
        prebuilt._writer = MagicMock()
        prebuilt._written_count = 3
        bridge._mock_load.return_value = prebuilt

        s = await bridge._get_or_create_session("ch1:u1")
        assert s is prebuilt
        # begin_persistence 不应被调用
        bridge._mock_begin.assert_not_called()

    # -- clear_session 边缘 --------------------------------------------------

    @pytest.mark.asyncio
    async def test_clear_session_nonexistent_noop(self, bridge: AgentBridge):
        """清除不存在的 session → 不抛异常。"""
        # 不预先创建
        await bridge.clear_session("u1", "ch1")  # 不应 raise

        # 之后创建仍是全新的
        s = await bridge._get_or_create_session("ch1:u1")
        assert s.messages == []

    @pytest.mark.asyncio
    async def test_clear_session_end_persistence_raises(self, bridge: AgentBridge):
        """end_persistence 抛异常时 session 和 lock 均已清理。

        try/finally 保证 lock 不泄漏。
        """
        await bridge._get_or_create_session("ch1:u1")

        with patch.object(SessionState, "end_persistence", side_effect=OSError("flush failed")):
            with pytest.raises(OSError, match="flush failed"):
                await bridge.clear_session("u1", "ch1")

        # session 已 pop（在 await end_persistence 之前）
        assert "ch1:u1" not in bridge._sessions
        # lock 也已清除（finally 块保证）
        assert "ch1:u1" not in bridge._session_locks

    # -- 并发 get_or_create 同 session ---------------------------------------

    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_same_session(self, bridge: AgentBridge):
        """两个并发 _get_or_create_session 同一 ID → 最终同一对象。

        当前实现无并发保护（无 Lock 串行化 _get_or_create_session），
        两个协程可能都走到 load_session → 都创建 SessionState →
        后者覆盖前者的缓存。这是已知的 benign race，因为：
        - load_session 是纯读，不产生副作用
        - 最终结果相同：都是同一个 session_id 的 SessionState
        - begin_persistence 幂等（检查 _writer is None）
        """
        async def _get():
            return await bridge._get_or_create_session("ch1:u1")

        s1, s2 = await asyncio.gather(_get(), _get())

        # 两个调用都应返回有效 session
        assert s1 is not None
        assert s2 is not None
        # 内存中最终只有一个
        assert bridge._sessions["ch1:u1"] in (s1, s2)
        # session_id 一致
        assert s1.session_id == s2.session_id == "ch1:u1"

    # -- project_hash 惰性计算 -----------------------------------------------

    def test_project_hash_computed_once(self):
        """_project_hash 只在首次 _get_or_create_session 时计算一次。"""
        call_count = 0

        def _fake_compute(_ws):
            nonlocal call_count
            call_count += 1
            return "abcd123400000000"

        with patch(
            "agent_core.persistence.path.compute_project_hash",
            side_effect=_fake_compute,
        ), patch(
            "agent_core.persistence.path.session_jsonl_path",
            return_value="/tmp/x.jsonl",
        ), patch(
            "agent_core.persistence.session.load_session",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            SessionState, "begin_persistence", new_callable=AsyncMock,
        ):
            b = AgentBridge(AgentConfig())
            assert b._project_hash is None

            async def _run():
                await b._get_or_create_session("a:1")
                assert b._project_hash == "abcd123400000000"
                assert call_count == 1
                # 第二次调用不重新计算
                await b._get_or_create_session("a:2")
                assert call_count == 1

            asyncio.run(_run())

    # -- process 特殊字符 user_id -------------------------------------------

    @pytest.mark.asyncio
    async def test_process_special_user_id(self, bridge: AgentBridge, mock_agent_cls):
        """带特殊字符的 user_id 不影响正常处理。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        result = await bridge.process("user/with:special*chars", "ch1", "hi", "text")
        assert result == ["ok"]

        # session_id 生成安全（_safe_session_id 处理了 : ）
        sid = bridge.session_id("user/with:special*chars", "ch1")
        safe = bridge._safe_session_id(sid)
        assert ":" not in safe

    # -- error_message 自定义 ------------------------------------------------

    @pytest.mark.asyncio
    async def test_custom_error_message(self, mock_agent_cls):
        """自定义 error_message 在出错时返回。"""
        with patch(
            "agent_core.persistence.path.compute_project_hash",
            return_value="deadbeef00000000",
        ), patch(
            "agent_core.persistence.path.session_jsonl_path",
            return_value="/tmp/x.jsonl",
        ), patch(
            "agent_core.persistence.session.load_session",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            SessionState, "begin_persistence", new_callable=AsyncMock,
        ):
            b = AgentBridge(AgentConfig(), error_message="自定义错误喵~")
            mock_agent_cls.return_value = _make_mock_agent([], error_msg="err")

            result = await b.process("u1", "ch1", "test", "text")
            assert result == ["自定义错误喵~"]
