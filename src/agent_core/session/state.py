"""
Session runtime state — the long-lived container for a single interactive
session (REPL run or SDK session).

The agent's per-prompt machinery (``PermissionEngine``, the ``query`` turn
loop) is rebuilt on every prompt, so any state that must survive *across*
prompts cannot live there.  ``SessionState`` is created once by the entry
point (``cli._repl``) and injected down into each (re-created) ``Agent`` /
``query``, carrying:

  - ``messages``         — conversation history, so prompt N sees prompts 1..N-1
  - ``permission_store`` — session-remembered allow/deny decisions

``transcript_path`` is reserved for the persistence milestone (Item 10);
in-memory continuity (this module) is intentionally independent of disk
persistence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..permissions.store import SessionPermissionStore


@dataclass
class SessionState:
    """Mutable runtime state for one interactive session."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict[str, Any]] = field(default_factory=list)
    permission_store: SessionPermissionStore = field(
        default_factory=SessionPermissionStore
    )

    # Reserved for Item 10 (persistence / resume); unused by the in-memory layer.
    transcript_path: str | None = None

    def snapshot_for_next_turn(self) -> list[dict[str, Any]]:
        """History to seed the next prompt's turn loop (``initial_messages``)."""
        return list(self.messages)

    def record_turn_result(self, final_messages: list[dict[str, Any]] | None) -> None:
        """Replace history with the turn's final message list.

        ``final_messages`` already contains the prior history plus everything
        produced this turn (and reflects any in-loop compression), so it is a
        full replacement rather than an append — avoiding double-counting.
        ``None`` (e.g. a stream that never reached ``done``) leaves history
        untouched.
        """
        if final_messages is not None:
            self.messages = list(final_messages)

    def clear_history(self) -> None:
        """Drop conversation history (e.g. a ``/clear`` command). Permission
        memory is left intact."""
        self.messages = []
