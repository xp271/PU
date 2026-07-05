from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

from .models import AgentConfig, AgentMessage, AgentResult, StopReason
from .permissions.engine import PermissionCallback
from .query import query
from .session import SessionState
from .hooks.registry import _default_hook_registry
from .persistence.path import compute_project_hash

__all__ = ["Agent", "PermissionCallback"]


def _sampled(trace_id: str, sample_rate: float) -> bool:
    """Deterministically decide whether to log this whole session.

    Hashes ``trace_id`` to a stable [0, 1) value and compares to
    ``sample_rate`` so a session is either fully logged or not at all.
    """
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < sample_rate


class Agent:
    def __init__(
        self, config: AgentConfig, session: SessionState | None = None
    ) -> None:
        self._config = config
        self._permission_callback: PermissionCallback | None = None
        self._trace_id = uuid.uuid4().hex
        # When provided, conversation history and session-permission memory
        # persist across prompts; when None, each run is independent.
        self._session = session
        # Item 12: conversation logging / data-collection (lazily created on
        # first stream when ``config.logging.enabled``).
        self._recorder: object | None = None
        self._sink: object | None = None

    def on_permission_check(self, callback: PermissionCallback) -> None:
        self._permission_callback = callback

    async def _ensure_recorder(self) -> None:
        """Create the log sink + recorder on demand (Item 12).

        No-op unless ``config.logging.enabled`` and this session passes the
        sampling gate.  Idempotent across prompts within one Agent.
        """
        if self._recorder is not None or not self._config.logging.enabled:
            return
        log_cfg = self._config.logging
        if not _sampled(self._trace_id, log_cfg.sample_rate):
            return

        from .logging.path import log_file_path
        from .logging.recorder import ConversationRecorder
        from .logging.redact import Redactor
        from .logging.sink import JsonlLogSink

        project_hash = compute_project_hash(self._config.workspace_root)
        session_id = self._session.session_id if self._session is not None else None
        path = log_file_path(
            project_hash,
            session_id,
            rolling=log_cfg.rolling,
            log_dir=log_cfg.log_dir,
            trace_id=self._trace_id,
        )
        sink = JsonlLogSink(
            path,
            flush_every=log_cfg.flush_every,
            queue_maxsize=log_cfg.queue_maxsize,
        )
        await sink.open()
        recorder = ConversationRecorder(
            sink,
            trace_id=self._trace_id,
            session_id=session_id,
            project_hash=project_hash,
            config=log_cfg,
            redactor=Redactor(log_cfg),
        )
        await recorder.record_session_start(
            self._config,
            resumed=bool(self._session is not None and self._session.messages),
        )
        self._sink = sink
        self._recorder = recorder

    async def aclose(self) -> None:
        """Release the log sink, draining its background write queue (Item 12).

        Safe to call when logging is disabled (no-op).  Call once at the end
        of an interactive session / SDK use so buffered records are flushed.
        """
        if self._sink is not None:
            await self._sink.aclose()  # type: ignore[attr-defined]
            self._sink = None
            self._recorder = None

    async def run(
        self, prompt: str, *, system: str | None = None
    ) -> AgentResult:
        content_parts: list[str] = []
        total_tokens = 0
        tool_uses = 0
        stop_reason: StopReason = "end_turn"
        error_detail: str | None = None

        async for message in self.stream(prompt, system=system):
            if message.type == "assistant_text":
                content_parts.append(message.content)
            elif message.type == "tool_use":
                tool_uses += 1
            elif message.type == "error":
                stop_reason = "error"
                error_detail = message.message
            elif message.type == "done":
                # Terminal message carries authoritative accounting.
                stop_reason = message.stop_reason
                total_tokens = message.total_tokens
                if message.error_detail is not None:
                    error_detail = message.error_detail

        return AgentResult(
            content="".join(content_parts),
            total_tokens=total_tokens,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            error_detail=error_detail,
        )

    async def stream(
        self, prompt: str, *, system: str | None = None
    ) -> AsyncGenerator[AgentMessage, None]:
        # ``aclosing`` guarantees the inner ``query`` generator is closed in
        # *this* task even if the consumer abandons us mid-stream — so MCP
        # teardown (query's finally) never runs at event-loop shutdown in a
        # different task (anyio cancel-scope requirement).
        session = self._session
        # §Item 12: conversation logging (bypass, read-only). Created on first
        # stream when enabled; ``self._recorder`` is None otherwise.
        await self._ensure_recorder()
        recorder = self._recorder
        if recorder is not None:
            await recorder.record_user_prompt(prompt)  # type: ignore[attr-defined]
        # §Item 9: per-query HookRegistry, derived from the read-only global
        # template and extended with this agent's config.hooks.
        hook_registry = _default_hook_registry.copy_with_overrides(
            self._config.hooks
        )
        async with aclosing(
            query(
                prompt=prompt,
                config=self._config,
                trace_id=self._trace_id,
                permission_callback=self._permission_callback,
                workspace_root=self._config.workspace_root,
                system=system,
                initial_messages=(
                    session.snapshot_for_next_turn() if session is not None else None
                ),
                session_store=session.permission_store if session is not None else None,
                hook_registry=hook_registry,
                session_id=session.session_id if session is not None else None,
                project_hash=(
                    compute_project_hash(self._config.workspace_root)
                    if session is not None
                    else None
                ),
                recorder=recorder,
            )
        ) as stream:
            async for message in stream:
                # Thread this turn's full history back into the session so the
                # next prompt continues the conversation.
                if session is not None and message.type == "done":
                    await session.flush_new_messages(message.final_messages)
                    session.record_turn_result(message.final_messages)
                # §Item 12: bypass-record every message (never mutates it).
                if recorder is not None:
                    await recorder.record_message(message)  # type: ignore[attr-defined]
                yield message
