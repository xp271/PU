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
    """连接微信客服信道和 agent-core 的 Agent。

    上下文管理策略：
    - agent-core 的 query/loop.py 已在每次 LLM 调用前自动执行 token 预算检查
      和上下文压缩（compress → hard_truncate），AgentBridge 无需重复实现
    - 会话惰性过期：仅在 process() 入口处检查，超时未活动的 session 直接移除
    - 被移除的 session 中丢失的对话记忆由微信用户自行发起新对话补偿
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

    # ------------------------------------------------------------------
    # Session 管理
    # ------------------------------------------------------------------

    @staticmethod
    def session_id(touser: str, open_kfid: str) -> str:
        """生成会话 ID。"""
        return f"{open_kfid}:{touser}"

    def _get_or_create_session(self, session_id: str) -> SessionState:
        """获取或创建 SessionState，同时惰性清理超时 session。

        惰性过期：仅在访问时检查并清理，不需要后台 task。
        """
        now = time.time()
        if session_id in self._sessions:
            last = self._sessions_last_access.get(session_id, 0.0)
            if now - last > self._session_timeout:
                self._sessions.pop(session_id, None)
                self._session_locks.pop(session_id, None)
                self._sessions_last_access.pop(session_id, None)
                logger.info(f"Session {session_id[:16]}... expired and removed")

        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
            self._session_locks[session_id] = asyncio.Lock()
        self._sessions_last_access[session_id] = now
        return self._sessions[session_id]

    def clear_session(self, user_id: str, open_kfid: str) -> None:
        """用户发送 /reset 或结束客服会话时清除会话。"""
        sid = self.session_id(user_id, open_kfid)
        self._sessions.pop(sid, None)
        self._session_locks.pop(sid, None)
        self._sessions_last_access.pop(sid, None)

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
        session = self._get_or_create_session(sid)
        lock = self._session_locks[sid]

        async with lock:
            prompt = self._build_prompt(content, msg_type)
            agent = Agent(self._base, session=session)

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
