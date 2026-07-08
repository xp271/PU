"""Agent 桥接层 — 连接 QQ 机器人信道和 agent-core Agent。

核心设计原则：
- SessionState 是长寿的（跨消息持久），存对话历史和权限记忆
- Agent 是短寿的（每次消息新建，用完即销毁）
- anyio cancel-scope 要求 Agent 的创建/消费/关闭在同一个 asyncio task 内
- 每个 session 持有 asyncio.Lock，防止同用户并发消息导致
  SessionState.record_turn_result() 全量替换时的竞态丢消息
- Session 生命周期 = 进程生命周期（无超时清理），重启后从磁盘 JSONL 恢复
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from agent_core.agent import Agent
from agent_core.models import AgentConfig
from agent_core.session import SessionState

from .text_splitter import split_text

logger = logging.getLogger("agent_core.bridge")


class AgentBridge:
    """连接 QQ 机器人信道和 agent-core 的 Agent。

    上下文管理策略：
    - agent-core 的 query/loop.py 已在每次 LLM 调用前自动执行 token 预算检查
      和上下文压缩（compress → hard_truncate），AgentBridge 无需重复实现
    - Session 在进程生命周期内常驻内存，重启后从磁盘 JSONL 恢复
    - 每次消息处理时 agent.py 自动调用 flush_new_messages 持久化到磁盘
    """

    def __init__(
        self,
        base_config: AgentConfig,
        system_prompt: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._base = base_config
        self._system_prompt = system_prompt
        self._error_message = error_message or "抱歉，处理您的请求时遇到了问题，请稍后再试。"
        self._sessions: dict[str, SessionState] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 持久化所需
        self._workspace_root = base_config.workspace_root
        self._project_hash: str | None = None  # 惰性计算

    # ------------------------------------------------------------------
    # Session 管理
    # ------------------------------------------------------------------

    @staticmethod
    def session_id(touser: str, open_kfid: str) -> str:
        """生成会话 ID。"""
        return f"{open_kfid}:{touser}"

    @staticmethod
    def _safe_session_id(bridge_session_id: str) -> str:
        """将 bridge 格式的 session_id（含 ``:``）转为文件安全的标识符。

        Windows 文件名不支持 ``:``，因此用 SHA-256 前 16 位 hex 替代。
        """
        return hashlib.sha256(bridge_session_id.encode()).hexdigest()[:16]

    async def _get_or_create_session(self, session_id: str) -> SessionState:
        """获取或创建 SessionState，从磁盘恢复历史（如有），激活持久化。

        流程：
        1. 惰性计算 project_hash
        2. 内存命中 → 直接返回（无超时检查——session 生命周期 = 进程生命周期）
        3. 尝试 load_session(safe_sid) 从磁盘恢复
        4. 磁盘无记录 → SessionState(session_id=session_id) 新建
        5. begin_persistence(tpath) 激活写入
        6. 存入内存缓存
        """
        from ..persistence.path import compute_project_hash, session_jsonl_path
        from ..persistence.session import load_session

        # 惰性计算 project_hash
        if self._project_hash is None:
            self._project_hash = compute_project_hash(self._workspace_root)

        # 内存命中（无超时检查——session 生命周期 = 进程生命周期）
        if session_id in self._sessions:
            return self._sessions[session_id]

        # 尝试从磁盘恢复
        safe_sid = self._safe_session_id(session_id)
        session = await load_session(
            safe_sid,
            project_hash=self._project_hash,
        )

        # 磁盘无记录 → 新建
        if session is None:
            session = SessionState(session_id=session_id)
        else:
            # load_session 返回的 session_id 是 safe_sid（hash），
            # 覆盖为 bridge 格式以保持日志/内存一致性
            session.session_id = session_id

        # 激活持久化（如果尚未激活；load_session 不调用 begin_persistence）
        if session._writer is None:  # noqa: SLF001
            tpath = session_jsonl_path(self._project_hash, safe_sid)
            await session.begin_persistence(tpath)

        # 缓存
        self._sessions[session_id] = session
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()

        logger.info(
            "Session %s: %d messages loaded (written_count=%d)",
            session_id[:32],
            len(session.messages),
            session._written_count,  # noqa: SLF001
        )
        return session

    async def clear_session(self, user_id: str, open_kfid: str) -> None:
        """用户发送 /reset 或结束客服会话时清除会话。"""
        sid = self.session_id(user_id, open_kfid)
        old = self._sessions.pop(sid, None)
        try:
            if old is not None:
                await old.end_persistence()
        finally:
            self._session_locks.pop(sid, None)

    async def get_session(self, user_id: str, open_kfid: str) -> "SessionState":
        """获取或创建指定用户的 SessionState（公开接口）。

        Channel 层可通过此方法访问 session，例如设置 workspace_override。
        """
        sid = self.session_id(user_id, open_kfid)
        return await self._get_or_create_session(sid)

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
        session = await self._get_or_create_session(sid)
        lock = self._session_locks[sid]

        async with lock:
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

        文本消息直接透传；语音消息优先使用语音识别文本；
        图片等其他类型附上媒体描述供 LLM 理解上下文。
        """
        if msg_type == "text":
            return content
        if msg_type == "voice":
            return f"[语音消息] {content}" if content else "[语音消息]"
        if msg_type == "image":
            return f"[图片消息] {content}" if content else "[图片消息]"
        return f"[{msg_type}消息] {content}" if content else f"[{msg_type}消息]"
