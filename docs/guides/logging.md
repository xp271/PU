# Conversation Logging & Data Collection

`agent-core` can record every conversation — user prompts, assistant text,
tool calls/results, per-turn token/latency metrics, errors and stop reasons —
to an append-only JSONL log, for **data collection / analysis** and **runtime
audit / observability**.

This layer is **separate** from session persistence (`--resume`):

| | Conversation logging (this guide) | Session persistence |
|---|---|---|
| Purpose | analysis, audit, data collection | resume / continue a conversation |
| Storage | `~/.agent/logs/` | `~/.agent/projects/` |
| Mutability | immutable, append-only | rewritten by compaction |
| Default | **off** (opt-in) | on (in CLI) |

It is a **read-only bypass**: it observes the message stream and never mutates
messages, token budget, or resume behaviour. Disabling it leaves Agent
behaviour byte-for-byte unchanged.

## Enabling

```python
from agent_core import Agent, AgentConfig, LoggingConfig

config = AgentConfig(
    model="anthropic/claude-sonnet-4-5",
    logging=LoggingConfig(
        enabled=True,             # opt-in
        rolling="per_session",    # or "per_day"
        record_tool_io=True,      # log tool inputs/results
        redact_keys=["api_key", "password"],   # masked as ***
        max_text_chars=50_000,    # truncate long text/content
        sample_rate=1.0,          # fraction of sessions to log
    ),
)

agent = Agent(config)
result = await agent.run("List the Python files")
await agent.aclose()             # drain the background write queue
```

Always call `await agent.aclose()` once you are done so the background write
queue is flushed. In the built-in REPL this happens automatically.

### Metrics-only mode

Set `record_content=False` to keep only `turn_metric` / `error` /
`session_*` records (no prompt or tool text) — pure observability with no
conversation content on disk.

## Log format

One JSON object per line. Every record shares an envelope
(`ts`, `seq`, `trace_id`, `session_id`, `project_hash`, `turn_index`) and a
`kind` discriminator:

`session_start`, `user_prompt`, `assistant_text`, `tool_use`, `tool_result`,
`turn_metric`, `error`, `session_end`.

Content events and `turn_metric` records share `trace_id` + `turn_index`, so
they can be joined during analysis.

## Reading & analysis

```python
from agent_core import load_log, iter_conversations, export_dataset, aggregate_metrics
from agent_core.logging.path import compute_project_hash, log_file_path

path = log_file_path(compute_project_hash(workspace_root), session_id)
records = await load_log(path)

# Structured, replayable conversations (audit)
for convo in iter_conversations(records):
    print(convo.trace_id, convo.stop_reason, len(convo.turns))

# Flat dataset for pandas / CSV (analysis)
rows = export_dataset(records, granularity="turn")  # or "message" / "session"

# Cost / latency / error summary (observability)
print(aggregate_metrics(records))
```

In the REPL, `/logs` prints the metric summary for the current session.

## Reliability

- Writes happen on a background task; `emit` never blocks the conversation.
- A full queue drops the oldest record and increments `sink.dropped` rather
  than stalling the turn loop.
- Any sink failure is swallowed and downgraded to a warning — a logging fault
  never interrupts the conversation.
- A crash leaves at most the last line incomplete; reopening isolates it.

## Retention

```python
from agent_core.logging.reader import cleanup_old_logs

removed = await cleanup_old_logs(workspace_root=ws, max_age_days=90)
```
