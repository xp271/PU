"""Agent 桥接层 — 连接微信客服信道和 agent-core Agent。

核心设计原则：
- SessionState 是长寿的（跨消息持久），存对话历史和权限记忆
- Agent 是短寿的（每次消息新建，用完即销毁）
- anyio cancel-scope 要求 Agent 的创建/消费/关闭在同一个 asyncio task 内
- 每个 session 持有 asyncio.Lock，防止同用户并发消息导致
  SessionState.record_turn_result() 全量替换时的竞态丢消息
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent_core.agent import Agent
from agent_core.models import AgentConfig
from agent_core.session import SessionState

from .text_splitter import split_text

logger = logging.getLogger("agent_core.bridge")


class AgentBridge:
    """连接信道（微信 KF / QQBot）和 agent-core 的 Agent。

    上下文管理策略：
    - agent-core 的 query/loop.py 已在每次 LLM 调用前自动执行 token 预算检查
      和上下文压缩（compress → hard_truncate），AgentBridge 无需重复实现
    - 会话惰性过期：仅在 process() 入口处检查，超时未活动的 session 直接移除
    - 被移除的 session 中丢失的对话记忆由用户自行发起新对话补偿
    """

    def __init__(
        self,
        base_config: AgentConfig,
        system_prompt: str | None = None,
        session_timeout: float = 3600.0,
        error_message: str | None = None,
    ) -> None:
        self._base = base_config
        self._system_prompt = system_prompt
        self._session_timeout = session_timeout
        self._error_message = error_message or "抱歉，处理您的请求时遇到了问题，请稍后再试。"
        self._sessions: dict[str, SessionState] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions_last_access: dict[str, float] = {}

        # Checkpoint callbacks (v1)
        self._on_session_created: list = []
        self._on_session_evicted: list = []

    # ------------------------------------------------------------------
    # Session 管理
    # ------------------------------------------------------------------

    @staticmethod
    def session_id(touser: str, open_kfid: str) -> str:
        """生成会话 ID。"""
        return f"{open_kfid}:{touser}"

    # -- checkpoint callback registration ---------------------------------

    def on_session_created(self, callback):
        """注册：新 session 创建时调用（在 ``process()`` 的 lock 内 await）。"""
        self._on_session_created.append(callback)

    def on_session_evicted(self, callback):
        """注册：session 被超时清理 / ``clear_session`` 前调用。"""
        self._on_session_evicted.append(callback)

    async def _fire_on_evicted(
        self, session_id: str, session: SessionState,
    ) -> None:
        """触发所有 evict 回调（异常隔离）。"""
        for cb in self._on_session_evicted:
            try:
                await cb(session_id, session)
            except Exception as exc:
                logger.error("on_session_evicted callback failed: %s", exc)

    async def _fire_on_created(
        self, session_id: str, session: SessionState,
    ) -> None:
        """触发所有 create 回调（异常隔离）。"""
        for cb in self._on_session_created:
            try:
                await cb(session_id, session)
            except Exception as exc:
                logger.error("on_session_created callback failed: %s", exc)

    # -- session management -----------------------------------------------

    def _get_or_create_session(
        self, session_id: str,
    ) -> tuple[SessionState, bool]:
        """获取或创建 SessionState，同时惰性清理超时 session。

        返回 ``(session, is_new)``。超时分支在 pop 前触发 evict 回调
        (fire-and-forget)。
        """
        now = time.time()
        is_new = False

        if session_id in self._sessions:
            last = self._sessions_last_access.get(session_id, 0.0)
            if now - last > self._session_timeout:
                old_session = self._sessions.pop(session_id, None)
                self._session_locks.pop(session_id, None)
                self._sessions_last_access.pop(session_id, None)
                if old_session is not None:
                    asyncio.ensure_future(
                        self._fire_on_evicted(session_id, old_session),
                    )
                logger.info(f"Session {session_id[:16]}... expired and removed")

        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
            self._session_locks[session_id] = asyncio.Lock()
            is_new = True
        self._sessions_last_access[session_id] = now
        return self._sessions[session_id], is_new

    def clear_session(self, user_id: str, open_kfid: str) -> None:
        """用户发送 /reset 或结束客服会话时清除会话。"""
        sid = self.session_id(user_id, open_kfid)
        old = self._sessions.pop(sid, None)
        self._session_locks.pop(sid, None)
        self._sessions_last_access.pop(sid, None)
        if old is not None:
            asyncio.ensure_future(self._fire_on_evicted(sid, old))

    def get_session(self, user_id: str, open_kfid: str) -> "SessionState":
        """获取或创建指定用户的 SessionState（公开接口）。

        Channel 层可通过此方法访问 session，例如设置 workspace_override。
        不触发 create 回调 — shell 指令（/cd、/ls 等）不需要 checkpoint 恢复。
        """
        sid = self.session_id(user_id, open_kfid)
        session, _ = self._get_or_create_session(sid)
        return session

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    async def process(
        self, user_id: str, open_kfid: str, content: str, msg_type: str
    ) -> list[str]:
        """处理一条用户消息，返回要发送的文本片段列表（已拆分）。

        每次消息新建 Agent，用完立即 aclose()，保证 anyio cancel-scope
        在同一个 asyncio task 内创建和销毁。

        并发安全：同用户的并发消息通过 per-session Lock 串行化，
        防止两个 Agent 同时写 SessionState 导致历史丢失。
        """
        sid = self.session_id(user_id, open_kfid)
        session, is_new = self._get_or_create_session(sid)
        lock = self._session_locks[sid]

        async with lock:
            # Fire create callback inside lock so checkpoint restore happens
            # before the first Agent turn processes the message.
            if is_new:
                await self._fire_on_created(sid, session)

            prompt = self._build_prompt(content, msg_type)

            # --- dynamic workspace override ---
            effective_config = self._base
            if session.workspace_override is not None:
                effective_config = self._base.model_copy(
                    update={"workspace_root": session.workspace_override}
                )
            # ---

            agent = Agent(effective_config, session=session)

            try:
                collected: list[str] = []
                error_msg: str | None = None
                async for msg in agent.stream(
                    prompt, system=self._system_prompt
                ):
                    if msg.type == "assistant_text":
                        collected.append(msg.content)
                    elif msg.type == "error":
                        error_msg = msg.message
                        logger.error(f"Agent error: {error_msg}")

                full_reply = "".join(collected)
                if not full_reply.strip():
                    if error_msg:
                        return [self._error_message]
                    return []
                return split_text(full_reply)
            finally:
                await agent.aclose()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(content: str, msg_type: str) -> str:
        """构建 Agent 输入 prompt。

        文本消息直接透传；语音消息优先使用微信语音识别的 Recognition 文本；
        图片等其他类型附上媒体描述供 LLM 理解上下文。
        """
        if msg_type == "text":
            return content
        if msg_type == "voice":
            return f"[语音消息] {content}" if content else "[语音消息]"
        if msg_type == "image":
            return f"[图片消息] {content}" if content else "[图片消息]"
        return f"[{msg_type}消息] {content}" if content else f"[{msg_type}消息]"

    # ------------------------------------------------------------------
    # Shutdown (v1): graceful exit — persist all active sessions
    # ------------------------------------------------------------------

    def shutdown(self) -> int:
        """同步关闭：遍历所有活跃 session，用同步 I/O 写入 checkpoint + JSONL。

        不依赖 asyncio event loop，可在 signal handler / finally 块中安全调用。
        只保存有消息的 session（空 session 跳过）。

        Returns:
            成功保存的 session 数量。
        """
        import json as _json
        import os as _os
        import uuid as _uuid
        from datetime import datetime as _datetime, timezone

        from agent_core.persistence.path import (
            checkpoint_json_path,
            compute_project_hash,
            session_jsonl_path,
        )

        saved = 0
        project_hash = compute_project_hash(self._base.workspace_root)

        for sid, session in list(self._sessions.items()):
            if not session.messages:
                continue
            try:
                ckpt_key = sid.replace(":", "_").replace("\\", "_").replace("/", "_")
                ckpt_path = checkpoint_json_path(project_hash, ckpt_key)
                jsonl_path = session_jsonl_path(project_hash, ckpt_key)
                _os.makedirs(_os.path.dirname(ckpt_path), exist_ok=True)

                now = _datetime.now().isoformat()
                if session._created_at is None:
                    session._created_at = now
                session._last_active_at = now

                # ---- 1) Save checkpoint ----
                data = session.checkpoint_dict()
                with open(ckpt_path, "w", encoding="utf-8") as fh:
                    _json.dump(data, fh, ensure_ascii=False, indent=2)

                # ---- 2) Sync-append unsaved messages to JSONL ----
                # Count existing entries (more reliable than _written_count
                # across multiple shutdowns / crashes).
                existing_count = 0
                last_uuid = None
                if _os.path.isfile(jsonl_path) and _os.path.getsize(jsonl_path) > 0:
                    with open(jsonl_path, "r", encoding="utf-8") as jf:
                        for line in jf:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            existing_count += 1
                            try:
                                entry = _json.loads(stripped)
                                last_uuid = entry.get("uuid")
                            except _json.JSONDecodeError:
                                pass

                new_messages = session.messages[existing_count:]
                if new_messages:
                    session_id = session.session_id
                    ts = _datetime.now(timezone.utc).isoformat()
                    with open(jsonl_path, "a", encoding="utf-8") as jf:
                        for msg in new_messages:
                            entry_uuid = _uuid.uuid4().hex
                            entry = {
                                "uuid": entry_uuid,
                                "parentUuid": last_uuid,
                                "sessionId": session_id,
                                "timestamp": ts,
                                "isSidechain": False,
                                "version": 1,
                                "type": "message",
                                "message": msg,
                            }
                            jf.write(
                                _json.dumps(entry, ensure_ascii=False) + "\n"
                            )
                            last_uuid = entry_uuid

                saved += 1
            except Exception:
                logger.exception(
                    "shutdown: failed to save checkpoint for %s", sid,
                )

        self._sessions.clear()
        return saved
