"""
Session persistence — JSONL transcript storage & recovery.

Public API
----------

**Path utilities**::

    from agent_core.persistence import (
        checkpoint_json_path,
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
        load_session_full,
        delete_session,
        cleanup_old_sessions,
    )

**Checkpoint**::

    from agent_core.persistence import (
        save_checkpoint,
        load_checkpoint,
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
    checkpoint_json_path,
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
    load_checkpoint,
    load_session,
    load_session_full,
    save_checkpoint,
)
from .storage import (
    CompactionRecord,
    SessionTranscriptWriter,
    TranscriptEntry,
)

__all__ = [
    # path
    "checkpoint_json_path",
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
    "load_session_full",
    "delete_session",
    "cleanup_old_sessions",
    # checkpoint
    "save_checkpoint",
    "load_checkpoint",
    # migrate
    "migrate_transcript",
]
