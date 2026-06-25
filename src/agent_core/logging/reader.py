"""
Reading, conversation reconstruction, and dataset export for the conversation
log layer.

Serves the two downstream uses: **data collection / analysis** (export a flat,
analysis-friendly dataset) and **audit / observability** (reconstruct
structured conversations, aggregate cost/latency metrics).
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

import aiofiles

from .events import LogRecord, LogRecordAdapter
from .path import compute_project_hash, list_log_files

logger = logging.getLogger("agent_core.logging.reader")

__all__ = [
    "load_log",
    "ConversationView",
    "TurnView",
    "iter_conversations",
    "export_dataset",
    "aggregate_metrics",
    "cleanup_old_logs",
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


async def load_log(path: str) -> list[LogRecord]:
    """Parse a JSONL log file into :class:`LogRecord` objects.

    Bad lines are skipped with a warning; a missing file yields ``[]``.
    """
    if not os.path.isfile(path):
        return []
    records: list[LogRecord] = []
    async with aiofiles.open(path, "r", encoding="utf-8") as fh:
        async for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(LogRecordAdapter.validate_python(json.loads(line)))
            except Exception as exc:
                logger.warning("skipping bad log line in %s: %s", path, exc)
    return records


# ---------------------------------------------------------------------------
# Conversation reconstruction (audit / replay)
# ---------------------------------------------------------------------------


@dataclass
class TurnView:
    turn_index: int
    assistant_text: str = ""
    tool_calls: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)
    metric: Any = None  # TurnMetricRecord | None


@dataclass
class ConversationView:
    trace_id: str
    session_id: str | None
    user_prompts: list[str] = field(default_factory=list)
    turns: list[TurnView] = field(default_factory=list)
    total_tokens: int = 0
    tool_uses_total: int = 0
    stop_reason: str | None = None


def iter_conversations(records: list[LogRecord]) -> list[ConversationView]:
    """Group records by ``trace_id`` (ordered by ``seq``) into structured
    conversation views, aligning content events with their turn metric."""
    by_trace: dict[str, list[LogRecord]] = defaultdict(list)
    for r in records:
        by_trace[r.trace_id].append(r)

    views: list[ConversationView] = []
    for trace_id, recs in by_trace.items():
        recs = sorted(recs, key=lambda r: r.seq)
        view = ConversationView(trace_id=trace_id, session_id=None)
        turns: dict[int, TurnView] = {}
        # Pending tool inputs keyed by tool_use_id so results can pair up.
        tool_inputs: dict[str, tuple[int, str, dict[str, Any]]] = {}

        def _turn(idx: int | None) -> TurnView:
            key = idx if isinstance(idx, int) else 0
            if key not in turns:
                turns[key] = TurnView(turn_index=key)
            return turns[key]

        for r in recs:
            kind = r.kind
            if view.session_id is None:
                view.session_id = r.session_id
            if kind == "user_prompt":
                view.user_prompts.append(r.text)
            elif kind == "assistant_text":
                t = _turn(r.turn_index)
                t.assistant_text += r.text
            elif kind == "tool_use":
                tool_inputs[r.tool_use_id] = (
                    r.turn_index if isinstance(r.turn_index, int) else 0,
                    r.tool_name,
                    r.tool_input,
                )
            elif kind == "tool_result":
                meta = tool_inputs.pop(r.tool_use_id, None)
                if meta is not None:
                    idx, name, tin = meta
                else:
                    idx, name, tin = (
                        r.turn_index if isinstance(r.turn_index, int) else 0,
                        r.tool_name,
                        {},
                    )
                _turn(idx).tool_calls.append((name, tin, r.content))
            elif kind == "turn_metric":
                _turn(r.turn_index).metric = r
            elif kind == "session_end":
                view.total_tokens = r.total_tokens
                view.tool_uses_total = r.tool_uses_total
                view.stop_reason = r.stop_reason

        view.turns = [turns[k] for k in sorted(turns)]
        views.append(view)
    return views


# ---------------------------------------------------------------------------
# Dataset export (analysis)
# ---------------------------------------------------------------------------


def export_dataset(
    records: list[LogRecord],
    *,
    granularity: Literal["turn", "message", "session"] = "turn",
) -> list[dict[str, Any]]:
    """Flatten records into one-row-per-unit dicts (CSV / DataFrame friendly)."""
    if granularity == "message":
        return [r.model_dump() for r in sorted(records, key=lambda r: (r.trace_id, r.seq))]

    convos = iter_conversations(records)

    if granularity == "session":
        rows: list[dict[str, Any]] = []
        for c in convos:
            in_tok = sum(t.metric.input_tokens for t in c.turns if t.metric)
            out_tok = sum(t.metric.output_tokens for t in c.turns if t.metric)
            rows.append(
                {
                    "trace_id": c.trace_id,
                    "session_id": c.session_id,
                    "prompts": len(c.user_prompts),
                    "turns": len(c.turns),
                    "tool_uses_total": c.tool_uses_total,
                    "total_tokens": c.total_tokens,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "stop_reason": c.stop_reason,
                }
            )
        return rows

    # granularity == "turn"
    rows = []
    for c in convos:
        for t in c.turns:
            m = t.metric
            rows.append(
                {
                    "trace_id": c.trace_id,
                    "session_id": c.session_id,
                    "turn_index": t.turn_index,
                    "assistant_text": t.assistant_text,
                    "tool_calls": [name for name, _in, _out in t.tool_calls],
                    "tool_call_count": len(t.tool_calls),
                    "provider": m.provider if m else None,
                    "model": m.model if m else None,
                    "input_tokens": m.input_tokens if m else None,
                    "output_tokens": m.output_tokens if m else None,
                    "latency_ms": m.latency_ms if m else None,
                    "status": m.status if m else None,
                    "error_type": m.error_type if m else None,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Metric aggregation (observability)
# ---------------------------------------------------------------------------


def aggregate_metrics(records: list[LogRecord]) -> dict[str, Any]:
    """Summarize token usage, latency, error rate and tool frequency."""
    metrics = [r for r in records if r.kind == "turn_metric"]
    tool_uses = [r for r in records if r.kind == "tool_use"]
    errors = [r for r in records if r.kind == "error"]

    input_tokens = sum(m.input_tokens for m in metrics)
    output_tokens = sum(m.output_tokens for m in metrics)
    latencies = [m.latency_ms for m in metrics if m.latency_ms is not None]
    latencies.sort()

    def _pct(p: float) -> float | None:
        if not latencies:
            return None
        idx = min(len(latencies) - 1, int(p * len(latencies)))
        return latencies[idx]

    by_provider: dict[str, int] = defaultdict(int)
    for m in metrics:
        by_provider[m.provider] += m.input_tokens + m.output_tokens

    by_tool: dict[str, int] = defaultdict(int)
    for tu in tool_uses:
        by_tool[tu.tool_name] += 1

    sessions = {r.trace_id for r in records}

    return {
        "sessions": len(sessions),
        "turns": len(metrics),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "tool_uses": len(tool_uses),
        "tool_frequency": dict(by_tool),
        "tokens_by_provider": dict(by_provider),
        "errors": len(errors),
        "error_rate": (len(errors) / len(metrics)) if metrics else 0.0,
        "latency_ms_avg": (sum(latencies) / len(latencies)) if latencies else None,
        "latency_ms_p50": _pct(0.50),
        "latency_ms_p95": _pct(0.95),
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def cleanup_old_logs(
    project_hash: str | None = None,
    *,
    workspace_root: str | None = None,
    max_age_days: int = 90,
    log_dir: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Delete log files older than ``max_age_days`` (independent of
    persistence cleanup).  Returns the removed file paths."""
    import time

    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)
    cutoff = time.time() - max_age_days * 86400
    removed: list[str] = []
    for path in list_log_files(project_hash, log_dir=log_dir):
        try:
            if os.path.getmtime(path) < cutoff:
                removed.append(path)
                if not dry_run:
                    os.remove(path)
        except OSError:
            continue
    return removed
