"""
Storage path computation — pure functions with no side effects.

All path calculations are centralized here; the rest of the persistence
layer imports from this module instead of inlining path logic.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_HOME = os.path.join(os.getcwd(), ".agent")
PROJECTS_DIR = "sessions"
CURRENT_VERSION = 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_project_hash(workspace_root: str | None = None) -> str:
    """SHA-256 first 16 hex chars → short project identifier.

    Priority: *workspace_root* → ``os.getcwd()``.
    The absolute path's UTF-8 bytes are hashed with SHA-256 and the first
    16 hex characters are returned.
    """
    root = workspace_root if workspace_root is not None else os.getcwd()
    abs_path = os.path.abspath(root)
    digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()
    return digest[:16]


def project_dir(project_hash: str) -> str:
    """``{cwd}/.agent/sessions/{project_hash}/``"""
    return os.path.join(AGENT_HOME, PROJECTS_DIR, project_hash)


def session_jsonl_path(project_hash: str, session_id: str) -> str:
    """``{cwd}/.agent/sessions/{project_hash}/{session_id}.jsonl``"""
    return os.path.join(project_dir(project_hash), f"{session_id}.jsonl")


def sidechain_dir(project_hash: str, session_id: str) -> str:
    """``{cwd}/.agent/sessions/{project_hash}/{session_id}/``"""
    return os.path.join(project_dir(project_hash), session_id)


def sidechain_jsonl_path(project_hash: str, session_id: str, agent_id: str) -> str:
    """``{cwd}/.agent/sessions/{project_hash}/{session_id}/{agent_id}.jsonl``"""
    return os.path.join(sidechain_dir(project_hash, session_id), f"{agent_id}.jsonl")


def checkpoint_json_path(project_hash: str, checkpoint_key: str) -> str:
    """``{cwd}/.agent/sessions/{project_hash}/{checkpoint_key}.checkpoint.json``

    *checkpoint_key* 可以是 ``session.session_id`` (CLI/UUID) 或经过 sanitize
    的 bridge session key (QQBot，例如 ``"c2c_user1"``)。
    """
    return os.path.join(project_dir(project_hash), f"{checkpoint_key}.checkpoint.json")


def list_session_files(project_hash: str) -> list[str]:
    """Return ``{session_id}.jsonl`` filenames sorted by *mtime* descending.

    Returns an empty list when the project directory does not exist.
    """
    d = project_dir(project_hash)
    if not os.path.isdir(d):
        return []

    result: list[tuple[float, str]] = []
    for name in os.listdir(d):
        if not name.endswith(".jsonl"):
            continue
        full = os.path.join(d, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        session_id = name[:-6]  # strip ".jsonl"
        result.append((mtime, session_id))

    result.sort(key=lambda x: x[0], reverse=True)
    return [sid for _, sid in result]
