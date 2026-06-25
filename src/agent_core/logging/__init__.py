"""
Structured logging + conversation data-collection layer.

The package ``__init__`` is intentionally **lazy** (PEP 562 ``__getattr__``):
importing it does not eagerly pull in ``tracer`` (which imports ``llm.events``
→ ``models``).  This keeps ``models.py`` free to import
``agent_core.logging.config`` for its ``LoggingConfig`` field without creating
a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # static-analysis only; not executed at runtime
    from .config import LoggingConfig, RollingPolicy
    from .events import LogRecord
    from .reader import (
        aggregate_metrics,
        cleanup_old_logs,
        export_dataset,
        iter_conversations,
        load_log,
    )
    from .recorder import ConversationRecorder
    from .sink import JsonlLogSink, LogSink, NullSink
    from .tracer import TurnTracer

# name → submodule that defines it
_EXPORTS: dict[str, str] = {
    "TurnTracer": "tracer",
    "LoggingConfig": "config",
    "RollingPolicy": "config",
    "LogRecord": "events",
    "LogSink": "sink",
    "NullSink": "sink",
    "JsonlLogSink": "sink",
    "ConversationRecorder": "recorder",
    "load_log": "reader",
    "iter_conversations": "reader",
    "export_dataset": "reader",
    "aggregate_metrics": "reader",
    "cleanup_old_logs": "reader",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod = importlib.import_module(f".{module}", __name__)
    return getattr(mod, name)


def __dir__() -> list[str]:
    return sorted(__all__)
