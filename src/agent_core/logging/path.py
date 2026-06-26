"""
Log file path computation — pure functions, no side effects.

This module is **independent of** ``persistence/path.py`` (different root
directory: ``~/.agent/logs`` vs ``~/.agent/projects``) so the two file
families stay physically isolated (invariant #2).  It *reuses*
``compute_project_hash`` from persistence to avoid a second hash
implementation (single source of truth).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..persistence.path import compute_project_hash  # reuse, single source
from .config import RollingPolicy

__all__ = [
    "compute_project_hash",
    "AGENT_LOGS_ROOT",
    "logs_root",
    "project_log_dir",
    "log_file_path",
    "list_log_files",
]

AGENT_LOGS_ROOT = os.path.join(os.path.expanduser("~"), ".agent", "logs")
"""Physically isolated from ``~/.agent/projects`` (persistence)."""


def logs_root(log_dir: str | None = None) -> str:
    """Resolve the log root: explicit *log_dir* override → default."""
    if log_dir is not None:
        return os.path.abspath(os.path.expanduser(log_dir))
    return AGENT_LOGS_ROOT


def project_log_dir(project_hash: str, *, log_dir: str | None = None) -> str:
    """``{root}/{project_hash}/``"""
    return os.path.join(logs_root(log_dir), project_hash)


def log_file_path(
    project_hash: str,
    session_id: str | None,
    *,
    rolling: RollingPolicy = "per_session",
    log_dir: str | None = None,
    trace_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Compute the JSONL log path.

    ``per_session``: ``{root}/{project_hash}/{name}.jsonl``
    ``per_day``:     ``{root}/{project_hash}/{YYYY-MM-DD}/{name}.jsonl``

    where ``name`` is ``session_id`` when present, else ``trace_id`` (so
    sessionless runs still get a stable file name).
    """
    name = session_id or trace_id or "session"
    base = project_log_dir(project_hash, log_dir=log_dir)
    if rolling == "per_day":
        day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        return os.path.join(base, day, f"{name}.jsonl")
    return os.path.join(base, f"{name}.jsonl")


def list_log_files(project_hash: str, *, log_dir: str | None = None) -> list[str]:
    """Return all ``*.jsonl`` log paths for *project_hash*, mtime descending.

    Walks one level deep to also catch ``per_day`` subdirectories.  Returns an
    empty list when the project log dir does not exist.
    """
    base = project_log_dir(project_hash, log_dir=log_dir)
    if not os.path.isdir(base):
        return []

    found: list[tuple[float, str]] = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            found.append((mtime, full))

    found.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in found]
