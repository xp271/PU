"""
Session persistence — JSONL transcript storage & recovery.

Public API
----------

**Path utilities**::

    from agent_core.persistence import (
        compute_project_hash,
        session_jsonl_path,
        sidechain_dir,
        sidechain_jsonl_path,
    )

**Core data models**::

    from agent_core.persistence import (
        TranscriptEntry,
        CompactionRecord,
        SessionTranscriptWriter,
    )

**Session CRUD**::

    from agent_core.persistence import (
        SessionInfo,
        list_sessions,
        load_session,
        delete_session,
        cleanup_old_sessions,
    )

**Recovery**::

    from agent_core.persistence import (
        clean_and_rebuild,
        RecoveryResult,
        TurnInterruption,
    )

**Migration**::

    from agent_core.persistence import migrate_transcript
"""

from .migrate import migrate_transcript
from .path import (
    compute_project_hash,
    session_jsonl_path,
    sidechain_dir,
    sidechain_jsonl_path,
)
from .recovery import (
    RecoveryResult,
    TurnInterruption,
    clean_and_rebuild,
)
from .session import (
    SessionInfo,
    cleanup_old_sessions,
    delete_session,
    list_sessions,
    load_session,
)
from .storage import (
    CompactionRecord,
    SessionTranscriptWriter,
    TranscriptEntry,
)

__all__ = [
    # path
    "compute_project_hash",
    "session_jsonl_path",
    "sidechain_dir",
    "sidechain_jsonl_path",
    # storage
    "TranscriptEntry",
    "CompactionRecord",
    "SessionTranscriptWriter",
    # recovery
    "RecoveryResult",
    "TurnInterruption",
    "clean_and_rebuild",
    # session
    "SessionInfo",
    "list_sessions",
    "load_session",
    "delete_session",
    "cleanup_old_sessions",
    # migrate
    "migrate_transcript",
]
