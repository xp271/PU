"""
Session CRUD — list, load (resume), delete, and cleanup of persistent
session transcript files.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

import aiofiles

from .migrate import migrate_transcript
from .path import (
    checkpoint_json_path,
    compute_project_hash,
    list_session_files,
    project_dir,
    session_jsonl_path,
    sidechain_dir,
)
from .recovery import clean_and_rebuild
from .storage import load_entries

if TYPE_CHECKING:
    from agent_core.session import SessionState

logger = logging.getLogger("agent_core.persistence.session")


# ---------------------------------------------------------------------------
# SessionInfo
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """Lightweight metadata about one persisted session."""

    session_id: str
    title: str  # First user message text, truncated to 100 chars
    created_at: str  # ISO timestamp
    last_activity: str  # ISO timestamp
    message_count: int
    project_hash: str
    has_unfinished_turn: bool = False
    workspace_override: str | None = None


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def _list_all_session_keys(project_hash: str) -> set[str]:
    """Scan project_dir for both .jsonl and .checkpoint.json, return
    the union of session keys (filename stems)."""
    d = project_dir(project_hash)
    if not os.path.isdir(d):
        return set()
    keys: set[str] = set()
    for name in os.listdir(d):
        if name.endswith(".checkpoint.json"):
            keys.add(name[: -len(".checkpoint.json")])
        elif name.endswith(".jsonl"):
            keys.add(name[: -len(".jsonl")])
    return keys


async def list_sessions(
    workspace_root: str | None = None,
) -> list[SessionInfo]:
    """Scan the project's transcript directory and return metadata for
    every session, sorted by last activity (most recent first).

    Dual-source scan: reads ``.checkpoint.json`` for fast metadata (O(1))
    when available; falls back to JSONL scanning for sessions without a
    checkpoint file.
    """
    project_hash = compute_project_hash(workspace_root)
    all_keys = _list_all_session_keys(project_hash)
    if not all_keys:
        return []

    result: list[SessionInfo] = []
    for key in sorted(all_keys):
        # 1) Try fast path: read checkpoint metadata
        ckpt = await load_checkpoint(key, project_hash=project_hash)
        if ckpt is not None:
            result.append(SessionInfo(
                session_id=key,
                title=ckpt.get("title", "(no title)"),
                created_at=str(ckpt.get("created_at", "")),
                last_activity=str(ckpt.get("last_active_at", "")),
                message_count=int(ckpt.get("message_count", 0)),
                project_hash=project_hash,
                workspace_override=ckpt.get("workspace_override"),
            ))
            continue

        # 2) Fallback: scan JSONL
        path = session_jsonl_path(project_hash, key)
        info = await _read_session_info(key, path, project_hash)
        if info is not None:
            result.append(info)

    result.sort(key=lambda x: x.last_activity, reverse=True)
    return result


async def _read_session_info(
    session_id: str, path: str, project_hash: str
) -> SessionInfo | None:
    """Extract ``SessionInfo`` from a single JSONL file.

    Reads:
    * First line → title (first user text) + created_at
    * Full file scan → message_count
    * Last entry → last_activity + has_unfinished_turn

    Returns ``None`` if the file is empty / unreadable / has no entries.
    """
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None

    try:
        title = "(empty)"
        created_at = ""
        last_activity = ""
        message_count = 0
        has_unfinished = False
        last_message_role: str = ""

        # Read first line for title + created_at
        async with aiofiles.open(path, "r", encoding="utf-8") as fh:
            first_line = await fh.readline()
            if first_line:
                try:
                    entry = json.loads(first_line.strip())
                except json.JSONDecodeError:
                    return None
                created_at = entry.get("timestamp", "")
                # Extract first user text from message envelope
                msg = _unwrap_message(entry)
                if msg and msg.get("role") == "user":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                t = b.get("text", "")
                                title = t[:100].replace("\n", " ")
                                break
                    elif isinstance(content, str):
                        title = content[:100].replace("\n", " ")

            # Full scan for count + last entry detection
            async for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                message_count += 1
            # Re-read last entry role
            last_line = ""
            async with aiofiles.open(path, "r", encoding="utf-8") as fh2:
                async for line in fh2:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped

            if last_line:
                try:
                    last_entry = json.loads(last_line)
                except json.JSONDecodeError:
                    last_entry = {}
                last_activity = last_entry.get("timestamp", created_at)
                last_msg = _unwrap_message(last_entry)
                if last_msg:
                    last_message_role = last_msg.get("role", "")
                    if last_message_role == "user":
                        # Check if user message contains tool_result
                        content = last_msg.get("content", [])
                        if isinstance(content, list):
                            has_tr = any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content
                            )
                            has_unfinished = not has_tr
                    elif last_message_role == "assistant":
                        # Check for pending tool_use
                        content = last_msg.get("content", [])
                        if isinstance(content, list):
                            has_unfinished = any(
                                isinstance(b, dict) and b.get("type") == "tool_use"
                                for b in content
                            )

        # Message count includes the first line (which readline consumed)
        # The for loop only counts remaining lines, so +1
        message_count += 1

        return SessionInfo(
            session_id=session_id,
            title=title,
            created_at=created_at,
            last_activity=last_activity,
            message_count=message_count,
            project_hash=project_hash,
            has_unfinished_turn=has_unfinished,
        )
    except Exception as exc:
        logger.debug("Failed to read session info for %s: %s", session_id, exc)
        return None


def _unwrap_message(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the message dict from a JSONL entry.

    Handles both v1 ``TranscriptEntry`` envelope and v0 bare message.
    """
    if "message" in entry:
        return entry["message"]  # type: ignore[no-any-return]
    if "role" in entry:
        return entry  # type: ignore[no-any-return]  # v0 bare format
    return None


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------


async def load_session(
    session_id: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> Any | None:
    """Restore a ``SessionState`` from disk.

    Flow:
    1. Resolve *project_hash*.
    2. Run ``migrate_transcript`` (idempotent, problem #6).
    3. ``load_entries`` → ``clean_and_rebuild``.
    4. Build ``SessionState`` with pre-set ``_written_count`` to prevent
       ``begin_persistence`` from re-appending already-stored history
       (problem #1).
    5. ``SessionPermissionStore`` is NOT restored from JSONL (v1 decision,
       problem #10).

    Returns ``None`` when the session does not exist or is unrecoverable.
    """
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    path = session_jsonl_path(project_hash, session_id)
    if not os.path.isfile(path):
        return None

    # Migrate before reading (problem #6 — idempotent, no-op if already v1)
    await migrate_transcript(path)

    entries = await load_entries(path)
    if not entries:
        logger.warning("load_session(%s): empty or corrupt file", session_id)
        return None

    result = await clean_and_rebuild(entries)
    if not result.messages:
        logger.warning(
            "load_session(%s): recovery produced empty messages", session_id
        )

    # Build SessionState with pre-set written_count (problem #1)
    from agent_core.session import SessionState

    session = SessionState(
        session_id=session_id,
        messages=result.messages,
    )
    session._written_count = result.written_count
    session._compaction_count = result.compaction_count
    session.transcript_path = path

    logger.info(
        "load_session(%s): %d entries → %d live messages (written_count=%d)",
        session_id,
        result.entries_loaded,
        result.entries_kept,
        result.written_count,
    )
    return session


# ---------------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------------


async def delete_session(
    session_id: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> bool:
    """Delete the session JSONL file and its sidechain subdirectory.

    Returns ``True`` if the session was deleted, ``False`` if it
    did not exist.
    """
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    path = session_jsonl_path(project_hash, session_id)
    sidechain = sidechain_dir(project_hash, session_id)

    deleted = False

    if os.path.isfile(path):
        os.unlink(path)
        deleted = True

    if os.path.isdir(sidechain):
        shutil.rmtree(sidechain, ignore_errors=True)

    return deleted


# ---------------------------------------------------------------------------
# cleanup_old_sessions
# ---------------------------------------------------------------------------


async def cleanup_old_sessions(
    workspace_root: str | None = None,
    *,
    max_age_days: int = 30,
    dry_run: bool = False,
) -> list[str]:
    """Delete sessions whose last activity is older than *max_age_days*.

    Returns the list of deleted ``session_id`` values.
    """
    sessions = await list_sessions(workspace_root)
    cutoff = _time.time() - (max_age_days * 86400)
    deleted: list[str] = []

    for s in sessions:
        # Parse last_activity ISO timestamp → epoch
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(s.last_activity)
            ts = dt.timestamp()
        except (ValueError, TypeError):
            continue

        if ts < cutoff:
            if not dry_run:
                await delete_session(
                    s.session_id, project_hash=s.project_hash
                )
            deleted.append(s.session_id)

    logger.info(
        "cleanup_old_sessions: %d sessions removed (dry_run=%s)",
        len(deleted),
        dry_run,
    )
    return deleted


# ---------------------------------------------------------------------------
# Checkpoint (v1): lightweight session snapshot for cross-process resume
# ---------------------------------------------------------------------------


async def save_checkpoint(
    session: "SessionState",
    *,
    checkpoint_key: str | None = None,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> bool:
    """Persist session metadata as ``.checkpoint.json`` alongside the ``.jsonl``.

    Idempotent — overwrites an existing file of the same name.
    Empty sessions (``messages=[]``) are still saved (so ``workspace_override``
    is preserved).

    *checkpoint_key* defaults to ``session.session_id``.
    Returns ``True`` on success.
    """
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    ckpt_key = checkpoint_key or session.session_id
    ckpt_path = checkpoint_json_path(project_hash, ckpt_key)

    os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)

    # Timestamps: created_at only on first save, last_active_at every save
    now = datetime.now().isoformat()
    if session._created_at is None:
        session._created_at = now
    session._last_active_at = now

    async with aiofiles.open(ckpt_path, "w", encoding="utf-8") as fh:
        await fh.write(
            json.dumps(
                session.checkpoint_dict(), ensure_ascii=False, indent=2,
            )
        )
    return True


async def load_checkpoint(
    checkpoint_key: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> dict[str, object] | None:
    """Read ``.checkpoint.json`` and return the raw dict, or ``None``
    if the file is missing or corrupt."""
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    ckpt_path = checkpoint_json_path(project_hash, checkpoint_key)
    if not os.path.isfile(ckpt_path):
        return None

    async with aiofiles.open(ckpt_path, "r", encoding="utf-8") as fh:
        raw = await fh.read()
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        logger.warning("Corrupted checkpoint: %s", ckpt_path)
        return None


async def load_session_full(
    session_id: str,
    *,
    checkpoint_key: str | None = None,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> "SessionState | None":
    """``load_session()`` + checkpoint restore.

    1. Restore ``messages`` + ``_written_count`` + ``_compaction_count``
       from JSONL (authoritative).
    2. Overlay ``workspace_override`` + ``permission_rules`` + timestamps
       from ``.checkpoint.json``.

    *checkpoint_key* defaults to *session_id*.
    """
    session = await load_session(
        session_id,
        workspace_root=workspace_root,
        project_hash=project_hash,
    )
    if session is None:
        return None

    ckpt_key = checkpoint_key or session_id
    ckpt = await load_checkpoint(
        ckpt_key,
        workspace_root=workspace_root,
        project_hash=project_hash,
    )
    if ckpt is not None:
        session.restore_checkpoint(ckpt)

    return session
