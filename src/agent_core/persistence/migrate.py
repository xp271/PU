"""
Format migration — version-driven automatic upgrade of transcript JSONL files.

``CURRENT_VERSION = 1``.

Migration chain:

    v0 → v1    Bare ``{role, content, ...}`` dicts (old sidechain format
               from ``agents/run.py``) are wrapped in ``TranscriptEntry``
               envelopes with auto-generated uuids and a linear
               ``parentUuid`` chain.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .storage import TranscriptEntry, load_entries, save_entries
from .path import CURRENT_VERSION

logger = logging.getLogger("agent_core.persistence.migrate")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def migrate_transcript(path: str) -> bool:
    """Read JSONL, detect version, upgrade in-place if needed.

    Returns:
        ``True``  — migration was performed.
        ``False`` — no migration needed (already at CURRENT_VERSION).

    Migration is *atomic* on top of ``save_entries`` (tempfile +
    ``os.replace``), so a crash during migration will not corrupt the
    original file.
    """
    # Read raw lines first for version detection
    raw_entries = await _read_raw_lines(path)
    if not raw_entries:
        return False

    if not needs_migration(raw_entries):
        return False

    # Detect version and apply upgrade chain
    version = _detect_version(raw_entries)
    logger.info("Migrating %s from v%d → v%d", path, version, CURRENT_VERSION)

    if version == 0:
        entries = _migrate_v0_to_v1(raw_entries)
    else:
        logger.warning(
            "Unknown migration path v%d → v%d, skipping", version, CURRENT_VERSION
        )
        return False

    await save_entries(path, entries)
    logger.info("Migration complete: %s (v%d)", path, CURRENT_VERSION)
    return True


def needs_migration(raw_entries: list[dict[str, Any]]) -> bool:
    """Check whether *raw_entries* need upgrading to ``CURRENT_VERSION``."""
    if not raw_entries:
        return False
    return _detect_version(raw_entries) < CURRENT_VERSION


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def _read_raw_lines(path: str) -> list[dict[str, Any]]:
    """Read a JSONL file as raw dicts (best-effort)."""
    import os as _os

    import aiofiles

    if not _os.path.isfile(path):
        return []
    result: list[dict[str, Any]] = []
    async with aiofiles.open(path, "r", encoding="utf-8") as fh:
        async for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                result.append(json.loads(stripped))
            except json.JSONDecodeError:
                logger.debug("Skipping unparseable line during migration scan")
    return result


def _detect_version(raw_entries: list[dict[str, Any]]) -> int:
    """Infer format version from the first parseable entry.

    - Has ``"uuid"`` key → at least v1
    - Otherwise → v0 (bare message dict)
    """
    for e in raw_entries:
        if "uuid" in e:
            return max(1, e.get("version", 1))
        return 0
    return 0


def _migrate_v0_to_v1(
    raw_entries: list[dict[str, Any]],
) -> list[TranscriptEntry]:
    """Wrap bare message dicts in ``TranscriptEntry`` envelopes.

    Each dict is treated as a ``message`` entry.  UUIDs are generated
    and ``parentUuid`` forms a linear chain.  ``sessionId`` is derived
    from the first entry if possible, otherwise generated.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    session_id = _uuid.uuid4().hex
    # Try to extract a sessionId from first entry
    if raw_entries:
        first = raw_entries[0]
        if isinstance(first, dict):
            sid = first.get("sessionId") or first.get("session_id", "")
            if sid:
                session_id = sid

    entries: list[TranscriptEntry] = []
    last_uuid: str | None = None
    ts = datetime.now(timezone.utc).isoformat()

    for raw in raw_entries:
        new_uuid = _uuid.uuid4().hex
        is_sidechain = False
        message = raw

        # If the raw dict already has a top-level "message" key, it might
        # be a partial v1 entry — unwrap and keep existing fields
        if "message" in raw and "uuid" not in raw:
            message = raw["message"]
            is_sidechain = raw.get("isSidechain", False)
            ts_raw = raw.get("timestamp", ts)
            entries.append(
                TranscriptEntry(
                    uuid=new_uuid,
                    parentUuid=last_uuid,
                    sessionId=raw.get("sessionId", session_id),
                    timestamp=ts_raw,
                    isSidechain=is_sidechain,
                    version=1,
                    type="message",
                    message=message,
                )
            )
        else:
            entries.append(
                TranscriptEntry(
                    uuid=new_uuid,
                    parentUuid=last_uuid,
                    sessionId=session_id,
                    timestamp=ts,
                    isSidechain=is_sidechain,
                    version=1,
                    type="message",
                    message=message,
                )
            )
        last_uuid = new_uuid

    return entries
