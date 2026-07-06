"""Tests for bridge/agent_bridge.py — AgentBridge session management + Agent lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.bridge.agent_bridge import AgentBridge
from agent_core.models import AgentConfig
from agent_core.session import SessionState


@pytest.fixture
def bridge() -> AgentBridge:
    return AgentBridge(AgentConfig(), session_timeout=0.1)


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


class TestSessionManagement:
    def test_session_id_format(self, bridge: AgentBridge):
        sid = bridge.session_id("user1", "channel1")
        assert sid == "channel1:user1"

    def test_session_creation(self, bridge: AgentBridge):
        s, is_new = bridge._get_or_create_session("ch1:u1")
        assert s is not None
        assert is_new is True

    def test_session_reuse(self, bridge: AgentBridge):
        s1, is_new1 = bridge._get_or_create_session("ch1:u1")
        s2, is_new2 = bridge._get_or_create_session("ch1:u1")
        assert s1 is s2
        assert is_new1 is True
        assert is_new2 is False

    def test_session_timeout_lazy_cleanup(self, bridge: AgentBridge):
        import time
        sid = "ch1:u1"
        s1, _ = bridge._get_or_create_session(sid)
        time.sleep(0.15)
        s2, is_new = bridge._get_or_create_session(sid)
        assert s1 is not s2
        assert is_new is True

    def test_clear_session(self, bridge: AgentBridge):
        bridge._get_or_create_session("ch1:u1")
        bridge.clear_session("u1", "ch1")
        s, is_new = bridge._get_or_create_session("ch1:u1")
        assert len(s.messages) == 0
        assert is_new is True

    def test_different_users_independent(self, bridge: AgentBridge):
        s1, _ = bridge._get_or_create_session("ch1:u1")
        s2, _ = bridge._get_or_create_session("ch1:u2")
        assert s1 is not s2
        assert bridge._session_locks["ch1:u1"] is not bridge._session_locks["ch1:u2"]


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


class TestGetSession:
    """AgentBridge.get_session() 公开接口测试。"""

    @pytest.mark.asyncio
    async def test_get_session_creates(self, bridge: AgentBridge):
        """get_session() 首次调用创建新 session。"""
        s = bridge.get_session("u1", "ch1")
        assert s is not None
        assert s.messages == []

    @pytest.mark.asyncio
    async def test_get_session_reuses(self, bridge: AgentBridge):
        """get_session() 重复调用返回同一个 session。"""
        s1 = bridge.get_session("u1", "ch1")
        s2 = bridge.get_session("u1", "ch1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_get_session_independent_users(self, bridge: AgentBridge):
        """不同用户有独立的 session。"""
        s1 = bridge.get_session("u1", "ch1")
        s2 = bridge.get_session("u2", "ch1")
        assert s1 is not s2


class TestWorkspaceOverride:
    """AgentBridge.process() 中 workspace_override 的测试。"""

    @pytest.mark.asyncio
    async def test_process_uses_workspace_override(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 非 None 时，Agent 收到覆盖后的 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = bridge.get_session("u1", "ch1")
        session.workspace_override = "D:\\test_ws"

        await bridge.process("u1", "ch1", "test", "text")

        # 验证传入 Agent 的 config.workspace_root 是覆盖后的值
        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]  # first positional arg = AgentConfig
        assert passed_config.workspace_root == "D:\\test_ws"

    @pytest.mark.asyncio
    async def test_process_no_override_no_change(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 为 None 时，Agent 使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        await bridge.process("u1", "ch1", "test", "text")

        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]
        # Should be the same as self._base (original, not overwritten)
        assert passed_config is bridge._base

    @pytest.mark.asyncio
    async def test_process_override_reset(self, bridge: AgentBridge, mock_agent_cls):
        """workspace_override 设为 None 后恢复使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = bridge.get_session("u1", "ch1")
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
# Checkpoint callbacks (v1)
# ---------------------------------------------------------------------------


class TestCheckpointCallbacks:
    @pytest.mark.asyncio
    async def test_on_session_created_callback(self, bridge: AgentBridge, mock_agent_cls):
        """New session triggers on_session_created callback inside lock."""
        created_sessions: list[str] = []
        async def _on_create(sid, session):
            created_sessions.append(sid)

        bridge.on_session_created(_on_create)
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        await bridge.process("u1", "ch1", "test", "text")
        assert len(created_sessions) == 1
        assert created_sessions[0] == "ch1:u1"

    @pytest.mark.asyncio
    async def test_on_session_evicted_callback_on_timeout(self, bridge: AgentBridge):
        """Timeout cleanup triggers on_session_evicted callback."""
        evicted: list[str] = []
        async def _on_evict(sid, session):
            evicted.append(sid)

        bridge.on_session_evicted(_on_evict)

        import time
        sid = "ch1:u1"
        session, _ = bridge._get_or_create_session(sid)
        session.messages.append({"role": "user", "content": "hello"})

        time.sleep(0.15)
        bridge._get_or_create_session(sid)  # triggers eviction

        # Give the fire-and-forget callback time to run
        await asyncio.sleep(0.1)
        assert sid in evicted

    @pytest.mark.asyncio
    async def test_on_session_evicted_callback_on_clear(self, bridge: AgentBridge):
        """clear_session triggers on_session_evicted callback."""
        evicted: list[str] = []
        async def _on_evict(sid, session):
            evicted.append(sid)

        bridge.on_session_evicted(_on_evict)
        bridge._get_or_create_session("ch1:u1")
        bridge.clear_session("u1", "ch1")

        await asyncio.sleep(0.1)
        assert "ch1:u1" in evicted

    @pytest.mark.asyncio
    async def test_checkpoint_callbacks_exception_isolation(self, bridge: AgentBridge, mock_agent_cls):
        """One callback raising an exception does not block others or crash process."""
        call_order: list[str] = []
        async def _good(sid, session):
            call_order.append("good")
        async def _bad(sid, session):
            call_order.append("bad")
            raise RuntimeError("callback bug")

        bridge.on_session_created(_bad)
        bridge.on_session_created(_good)
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        # Should not crash
        await bridge.process("u1", "ch1", "test", "text")
        assert "good" in call_order
        assert "bad" in call_order


# ---------------------------------------------------------------------------
# Shutdown (v1): graceful exit checkpoint persistence
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_empty_sessions(self, bridge: AgentBridge):
        """No active sessions → shutdown returns 0."""
        assert bridge.shutdown() == 0

    def test_shutdown_saves_checkpoints(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """shutdown writes .checkpoint.json for sessions with messages."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)

        # Set workspace_root so compute_project_hash uses tmp_path
        bridge._base.workspace_root = ws

        sid = "ch1:u1"
        session, _ = bridge._get_or_create_session(sid)
        session.messages.append(
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        )

        saved = bridge.shutdown()
        assert saved == 1

        # Verify checkpoint file exists and contains correct data
        project_hash = ppath.compute_project_hash(ws)
        ckpt_path = ppath.checkpoint_json_path(
            project_hash, "ch1_u1",
        )
        assert os.path.isfile(ckpt_path)
        with open(ckpt_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["workspace_override"] is None
        assert data["message_count"] == 1

    def test_shutdown_sanitizes_session_id(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """session_id with ':', '\', '/' is sanitized to '_' in filename."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        sid = "group123:user/abc\\def"
        session, _ = bridge._get_or_create_session(sid)
        session.messages.append(
            {"role": "user", "content": "hi"},
        )

        bridge.shutdown()

        project_hash = ppath.compute_project_hash(ws)
        ckpt_path = ppath.checkpoint_json_path(
            project_hash, "group123_user_abc_def",
        )
        assert os.path.isfile(ckpt_path)

    def test_shutdown_skips_empty_sessions(self, bridge: AgentBridge):
        """Sessions without messages are skipped."""
        sid1 = "ch1:u1"
        sid2 = "ch1:u2"
        s1, _ = bridge._get_or_create_session(sid1)
        s1.messages.append({"role": "user", "content": "present"})
        bridge._get_or_create_session(sid2)  # no messages

        saved = bridge.shutdown()
        assert saved == 1  # only sid1 was saved

    def test_shutdown_clears_sessions_after_save(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """After shutdown, _sessions dict is empty."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append({"role": "user", "content": "hi"})

        bridge.shutdown()
        assert len(bridge._sessions) == 0

    def test_shutdown_exception_isolation(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """One session failing to save doesn't block others."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        s1, _ = bridge._get_or_create_session("ch1:u1")
        s1.messages.append({"role": "user", "content": "first"})
        s2, _ = bridge._get_or_create_session("ch1:u2")
        s2.messages.append({"role": "user", "content": "second"})

        # Make one session's checkpoint_dict() fail
        original_dict = s1.checkpoint_dict
        def _broken():
            raise RuntimeError("simulated failure")
        s1.checkpoint_dict = _broken

        try:
            saved = bridge.shutdown()
            # s2 should still be saved despite s1 failure
            assert saved >= 1
        finally:
            s1.checkpoint_dict = original_dict

    def test_shutdown_idempotent(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """Second shutdown call returns 0 (sessions already cleared)."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append({"role": "user", "content": "hi"})

        first = bridge.shutdown()
        assert first == 1
        second = bridge.shutdown()
        assert second == 0

    def test_shutdown_with_workspace_override(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """Checkpoint includes workspace_override when set."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append({"role": "user", "content": "hi"})
        session.workspace_override = "/custom/path"

        bridge.shutdown()

        project_hash = ppath.compute_project_hash(ws)
        ckpt_path = ppath.checkpoint_json_path(project_hash, "ch1_u1")
        with open(ckpt_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["workspace_override"] == "/custom/path"

    def test_shutdown_with_permission_rules(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """Checkpoint includes session permission_rules."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append({"role": "user", "content": "hi"})
        session.permission_store.add_allow("Bash")
        session.permission_store.add_deny("Write")

        bridge.shutdown()

        project_hash = ppath.compute_project_hash(ws)
        ckpt_path = ppath.checkpoint_json_path(project_hash, "ch1_u1")
        with open(ckpt_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert len(data["permission_rules"]) == 2
        actions = {r["action"] for r in data["permission_rules"]}
        assert actions == {"allow", "deny"}

    def test_shutdown_none_workspace_root(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """shutdown works when workspace_root is None (fallback to os.getcwd())."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        # Don't set workspace_root — leave it as None (default)
        bridge._base.workspace_root = None

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append({"role": "user", "content": "hi"})

        saved = bridge.shutdown()
        assert saved == 1

        # Verify checkpoint was written using the cwd-based project_hash
        project_hash = ppath.compute_project_hash(None)
        ckpt_path = ppath.checkpoint_json_path(project_hash, "ch1_u1")
        assert os.path.isfile(ckpt_path)

    def test_shutdown_title_extraction(self, bridge: AgentBridge, tmp_path, monkeypatch):
        """Checkpoint title is extracted from first user message."""
        from agent_core.persistence import path as ppath

        home = tmp_path / "agent_home"
        monkeypatch.setattr(ppath, "AGENT_HOME", str(home))
        ws = str(tmp_path / "proj")
        os.makedirs(ws, exist_ok=True)
        bridge._base.workspace_root = ws

        session, _ = bridge._get_or_create_session("ch1:u1")
        session.messages.append(
            {"role": "user", "content": [{"type": "text", "text": "帮我分析bug"}]},
        )
        session.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        )

        bridge.shutdown()

        project_hash = ppath.compute_project_hash(ws)
        ckpt_path = ppath.checkpoint_json_path(project_hash, "ch1_u1")
        with open(ckpt_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["title"] == "帮我分析bug"
        assert data["message_count"] == 2
