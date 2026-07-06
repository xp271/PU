"""Tests for graceful shutdown — CLI double Ctrl+C + QQBot signal handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# TestCliDoubleCtrlC — _repl() 双 Ctrl+C 计时逻辑
# ═══════════════════════════════════════════════════════════════════════


class TestCliDoubleCtrlC:
    """通过 mock _repl() 的依赖来测试双 Ctrl+C 的计时和退出逻辑。"""

    def _make_session_mock(self):
        s = MagicMock()
        s.messages = []
        s.session_id = "test-session-id"
        s.workspace_override = None
        s.begin_persistence = AsyncMock()
        s.end_persistence = AsyncMock()
        return s

    def _make_agent_mock(self, raise_on_iterate=False):
        mock = MagicMock()
        mock.aclose = AsyncMock()
        if raise_on_iterate:
            async def _cancel_generator(prompt, system=None):
                raise KeyboardInterrupt
                yield  # unreachable, makes it an async generator
            mock.stream = _cancel_generator
        else:
            async def _normal(prompt, system=None):
                yield MagicMock(
                    type="done", stop_reason="end_turn", total_tokens=10,
                )
            mock.stream = _normal
        return mock

    @pytest.mark.asyncio
    async def test_double_ctrlc_within_2s_exits(self, monkeypatch):
        """Agent 推理中 2 秒内连续两次 Ctrl+C -> break 退出循环。"""
        from agent_core.models import AgentConfig

        config = AgentConfig()

        # First Agent throws KeyboardInterrupt, second also throws
        # but double Ctrl+C within 2s -> break from loop
        agent_calls = 0

        def _make_agent(*args, **kwargs):
            nonlocal agent_calls
            agent_calls += 1
            return self._make_agent_mock(raise_on_iterate=True)

        # Time: 0.0 (first cancel), 0.5 (second cancel, <2s) -> exit
        fake_times = [0.0, 0.5, 99.0]
        def _fake_monotonic():
            return fake_times.pop(0) if fake_times else 99.0

        monkeypatch.setattr(
            "agent_core.cli._main._time_module.monotonic", _fake_monotonic,
        )

        # Use readline path (not input) to avoid pytest capture conflict
        monkeypatch.setattr("agent_core.cli._main.sys.stdin.isatty", lambda: False)
        readline_calls = 0

        def _mock_readline():
            nonlocal readline_calls
            readline_calls += 1
            if readline_calls == 1:
                return "first prompt"
            return ""  # empty -> EOF -> break

        monkeypatch.setattr(
            "agent_core.cli._main.sys.stdin.readline", _mock_readline,
        )

        with patch("agent_core.cli._main.Agent", side_effect=_make_agent), \
             patch("agent_core.cli._main.SessionState") as mock_sess_cls, \
             patch("agent_core.cli._main._interactive_session_picker", return_value=None), \
             patch("agent_core.persistence.session.save_checkpoint", new_callable=AsyncMock) as mock_save, \
             patch("agent_core.persistence.path.compute_project_hash", return_value="deadbeef00000000"), \
             patch("agent_core.persistence.path.session_jsonl_path", return_value="/fake/path.jsonl"), \
             patch("agent_core.cli._main._handle_command", return_value=False), \
             patch("agent_core.cli._main.print"):
            mock_sess_cls.return_value = self._make_session_mock()

            from agent_core.cli._main import _repl

            await _repl(config, resume_id=None, continue_last=False)

            # First cancel + second cancel within 2s -> break -> finally saves
            assert mock_save.called

    @pytest.mark.asyncio
    async def test_two_ctrlc_over_2s_apart_does_not_exit(self, monkeypatch):
        """两次 Ctrl+C 间隔超过 2 秒 -> 只取消本轮，不退出。"""
        from agent_core.models import AgentConfig

        config = AgentConfig()

        agent_calls = 0

        def _make_agent(*args, **kwargs):
            nonlocal agent_calls
            agent_calls += 1
            if agent_calls <= 2:
                return self._make_agent_mock(raise_on_iterate=True)
            return self._make_agent_mock()  # normal completion

        # Time: 0.0, 3.0 (>2s), 10.0 (normal), 20.0 (quit timer)
        fake_times = [0.0, 3.0, 10.0, 20.0, 30.0]
        def _fake_monotonic():
            return fake_times.pop(0) if fake_times else 99.0

        monkeypatch.setattr(
            "agent_core.cli._main._time_module.monotonic", _fake_monotonic,
        )

        monkeypatch.setattr("agent_core.cli._main.sys.stdin.isatty", lambda: False)
        readline_calls = 0

        def _mock_readline():
            nonlocal readline_calls
            readline_calls += 1
            if readline_calls <= 3:
                return f"prompt {readline_calls}"
            return "/quit\n"

        monkeypatch.setattr(
            "agent_core.cli._main.sys.stdin.readline", _mock_readline,
        )

        with patch("agent_core.cli._main.Agent", side_effect=_make_agent), \
             patch("agent_core.cli._main.SessionState") as mock_sess_cls, \
             patch("agent_core.cli._main._interactive_session_picker", return_value=None), \
             patch("agent_core.persistence.session.save_checkpoint", new_callable=AsyncMock) as mock_save, \
             patch("agent_core.persistence.path.compute_project_hash", return_value="deadbeef00000000"), \
             patch("agent_core.persistence.path.session_jsonl_path", return_value="/fake/path.jsonl"), \
             patch("agent_core.cli._main._handle_command", return_value=True), \
             patch("agent_core.cli._main.print"):
            mock_sess_cls.return_value = self._make_session_mock()

            from agent_core.cli._main import _repl

            await _repl(config, resume_id=None, continue_last=False)

            assert mock_save.called
            assert agent_calls == 3

    @pytest.mark.asyncio
    async def test_timer_resets_after_successful_turn(self, monkeypatch):
        """正常完成一轮后计时器重置 -> 下一次单次 Ctrl+C 不会触发退出。"""
        from agent_core.models import AgentConfig

        config = AgentConfig()

        agent_calls = 0
        cancel_happened = False

        def _make_agent(*args, **kwargs):
            nonlocal agent_calls, cancel_happened
            agent_calls += 1
            if agent_calls == 2:
                async def _cancel_stream(prompt, system=None):
                    nonlocal cancel_happened
                    cancel_happened = True
                    raise KeyboardInterrupt
                    yield
                mock = MagicMock()
                mock.aclose = AsyncMock()
                mock.stream = _cancel_stream
                return mock
            return self._make_agent_mock()

        # Time: first cancel at 5.0, second at 100.0 (far apart),
        # but timer reset to 0.0 after first turn completed
        fake_times = [5.0, 100.0, 200.0, 300.0]
        def _fake_monotonic():
            return fake_times.pop(0) if fake_times else 999.0

        monkeypatch.setattr(
            "agent_core.cli._main._time_module.monotonic", _fake_monotonic,
        )

        monkeypatch.setattr("agent_core.cli._main.sys.stdin.isatty", lambda: False)
        readline_calls = 0

        def _mock_readline():
            nonlocal readline_calls
            readline_calls += 1
            if readline_calls <= 3:
                return f"prompt {readline_calls}"
            return "/quit\n"

        monkeypatch.setattr(
            "agent_core.cli._main.sys.stdin.readline", _mock_readline,
        )

        with patch("agent_core.cli._main.Agent", side_effect=_make_agent), \
             patch("agent_core.cli._main.SessionState") as mock_sess_cls, \
             patch("agent_core.cli._main._interactive_session_picker", return_value=None), \
             patch("agent_core.persistence.session.save_checkpoint", new_callable=AsyncMock) as mock_save, \
             patch("agent_core.persistence.path.compute_project_hash", return_value="deadbeef00000000"), \
             patch("agent_core.persistence.path.session_jsonl_path", return_value="/fake/path.jsonl"), \
             patch("agent_core.cli._main._handle_command", return_value=True), \
             patch("agent_core.cli._main.print"):
            mock_sess_cls.return_value = self._make_session_mock()

            from agent_core.cli._main import _repl

            await _repl(config, resume_id=None, continue_last=False)

            assert cancel_happened
            assert mock_save.called

    @pytest.mark.asyncio
    async def test_single_ctrlc_during_input_exits(self, monkeypatch):
        """input() 中 Ctrl+C -> break -> finally 保存 checkpoint。"""
        from agent_core.models import AgentConfig

        config = AgentConfig()

        monkeypatch.setattr("agent_core.cli._main.sys.stdin.isatty", lambda: False)

        def _mock_readline():
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "agent_core.cli._main.sys.stdin.readline", _mock_readline,
        )

        with patch("agent_core.cli._main.Agent"), \
             patch("agent_core.cli._main.SessionState") as mock_sess_cls, \
             patch("agent_core.cli._main._interactive_session_picker", return_value=None), \
             patch("agent_core.persistence.session.save_checkpoint", new_callable=AsyncMock) as mock_save, \
             patch("agent_core.persistence.path.compute_project_hash", return_value="deadbeef00000000"), \
             patch("agent_core.persistence.path.session_jsonl_path", return_value="/fake/path.jsonl"), \
             patch("agent_core.cli._main.print"):
            mock_sess_cls.return_value = self._make_session_mock()

            from agent_core.cli._main import _repl

            await _repl(config, resume_id=None, continue_last=False)

            assert mock_save.called


# ═══════════════════════════════════════════════════════════════════════
# TestQqBotSignalHandler — QQBot signal 处理和 finally 路径
# ═══════════════════════════════════════════════════════════════════════


class TestQqBotSignalHandler:
    """QQBot 的 _cmd_start 函数：signal handler 幂等 + finally 持久化。"""

    @pytest.mark.asyncio
    async def test_finally_calls_shutdown_after_keyboard_interrupt(self):
        """channel.start() 抛 KeyboardInterrupt -> finally 调 bridge.shutdown()。"""
        from agent_core.cli.qqbot import _cmd_start

        with patch("agent_core.cli.qqbot._load_qqbot_config") as mock_load_cfg, \
             patch("agent_core.cli.qqbot._setup_logging"), \
             patch("agent_core.cli.qqbot._load_agent_config"), \
             patch("agent_core.channels.qq_bot.channel.QQBotChannel") as mock_ch_cls, \
             patch("signal.signal"), \
             patch("agent_core.cli.qqbot.sys") as mock_sys:

            mock_ch = MagicMock()
            mock_ch.start = AsyncMock(side_effect=KeyboardInterrupt)
            mock_ch._bridge = MagicMock()
            mock_ch._bridge.shutdown = MagicMock(return_value=0)
            mock_ch_cls.return_value = mock_ch

            mock_config = MagicMock()
            mock_config.app_id = "test12345678"
            mock_load_cfg.return_value = (mock_config, None)

            try:
                await _cmd_start(None, {})
            except SystemExit:
                pass

            mock_ch._bridge.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_finally_calls_shutdown_after_exception(self):
        """channel.start() 抛异常 -> finally 仍调 bridge.shutdown()。"""
        from agent_core.cli.qqbot import _cmd_start

        with patch("agent_core.cli.qqbot._load_qqbot_config") as mock_load_cfg, \
             patch("agent_core.cli.qqbot._setup_logging"), \
             patch("agent_core.cli.qqbot._load_agent_config"), \
             patch("agent_core.channels.qq_bot.channel.QQBotChannel") as mock_ch_cls, \
             patch("signal.signal"), \
             patch("agent_core.cli.qqbot.sys") as mock_sys:

            mock_ch = MagicMock()
            mock_ch.start = AsyncMock(side_effect=RuntimeError("server error"))
            mock_ch._bridge = MagicMock()
            mock_ch._bridge.shutdown = MagicMock(return_value=0)
            mock_ch_cls.return_value = mock_ch

            mock_config = MagicMock()
            mock_config.app_id = "test12345678"
            mock_load_cfg.return_value = (mock_config, None)

            await _cmd_start(None, {})

            mock_sys.exit.assert_called_once_with(1)
            mock_ch._bridge.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_signal_handler_registered(self):
        """_cmd_start 注册了 SIGINT 和 SIGTERM 信号处理器。"""
        from agent_core.cli.qqbot import _cmd_start

        registered_signals: list[int] = []

        def _fake_signal(signum, handler):
            registered_signals.append(signum)

        with patch("agent_core.cli.qqbot._load_qqbot_config") as mock_load_cfg, \
             patch("agent_core.cli.qqbot._setup_logging"), \
             patch("agent_core.cli.qqbot._load_agent_config"), \
             patch("agent_core.channels.qq_bot.channel.QQBotChannel") as mock_ch_cls, \
             patch("signal.signal", side_effect=_fake_signal), \
             patch("agent_core.cli.qqbot.sys"):

            mock_ch = MagicMock()
            mock_ch.start = AsyncMock(side_effect=KeyboardInterrupt)
            mock_ch._bridge = MagicMock()
            mock_ch._bridge.shutdown = MagicMock(return_value=0)
            mock_ch_cls.return_value = mock_ch

            mock_config = MagicMock()
            mock_config.app_id = "test12345678"
            mock_load_cfg.return_value = (mock_config, None)

            try:
                await _cmd_start(None, {})
            except SystemExit:
                pass

            # SIGINT(2) and SIGTERM(15) on Windows; SIGINT is 2 on all platforms
            import signal
            assert signal.SIGINT in registered_signals
            assert signal.SIGTERM in registered_signals

    @pytest.mark.asyncio
    async def test_shutting_down_flag_prevents_double_execution(self):
        """_shutting_down 标志防止信号处理器被重复调用。"""
        from agent_core.cli.qqbot import _cmd_start

        registered_handler = None

        def _fake_signal(signum, handler):
            nonlocal registered_handler
            registered_handler = handler

        signal_called = asyncio.Event()

        async def _fake_start():
            await signal_called.wait()
            raise KeyboardInterrupt

        with patch("agent_core.cli.qqbot._load_qqbot_config") as mock_load_cfg, \
             patch("agent_core.cli.qqbot._setup_logging"), \
             patch("agent_core.cli.qqbot._load_agent_config"), \
             patch("agent_core.channels.qq_bot.channel.QQBotChannel") as mock_ch_cls, \
             patch("signal.signal", side_effect=_fake_signal), \
             patch("agent_core.cli.qqbot.sys"):

            mock_ch = MagicMock()
            mock_ch.start = _fake_start
            mock_ch._bridge = MagicMock()
            mock_ch._bridge.shutdown = MagicMock(return_value=0)
            mock_ch_cls.return_value = mock_ch

            mock_config = MagicMock()
            mock_config.app_id = "test12345678"
            mock_load_cfg.return_value = (mock_config, None)

            task = asyncio.ensure_future(_cmd_start(None, {}))
            await asyncio.sleep(0.01)

            # Call signal handler twice — second call should be no-op
            assert registered_handler is not None
            registered_handler(2, None)  # first -> _shutting_down = True
            registered_handler(2, None)  # second -> no-op (already True)

            signal_called.set()

            try:
                await task
            except SystemExit:
                pass

            # shutdown() called exactly once (from finally, not from signal handler)
            mock_ch._bridge.shutdown.assert_called_once()
