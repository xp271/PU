"""
Conversation logging / data-collection example (Item 12).

Records the full conversation (prompts, assistant text, tool calls, per-turn
metrics) to an append-only JSONL log, then reads it back for analysis.

Run:  set ANTHROPIC_API_KEY=... && python examples/conversation_logging.py
"""

from __future__ import annotations

import asyncio
import os

from agent_core import (
    Agent,
    AgentConfig,
    LoggingConfig,
    aggregate_metrics,
    iter_conversations,
    load_log,
)
from agent_core.logging.path import compute_project_hash, log_file_path


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    workspace = os.getcwd()

    config = AgentConfig(
        model="anthropic/claude-haiku-4-5",
        api_keys={"anthropic": api_key} if api_key else {},
        max_turns=5,
        workspace_root=workspace,
        logging=LoggingConfig(
            enabled=True,
            rolling="per_session",
            redact_keys=["api_key", "password"],
        ),
    )

    agent = Agent(config)
    result = await agent.run("Name two prime numbers below 10.")
    print(f"Response: {result.content}\n")

    # Flush the background write queue before reading the log back.
    await agent.aclose()

    # Locate and read the log written for this run.
    path = log_file_path(
        compute_project_hash(workspace),
        None,
        rolling="per_session",
        trace_id=agent._trace_id,
    )
    records = await load_log(path)
    print(f"Log file: {path}")
    print(f"Records:  {[r.kind for r in records]}\n")

    for convo in iter_conversations(records):
        print(f"Conversation {convo.trace_id[:8]}  stop={convo.stop_reason}")
        for turn in convo.turns:
            tools = [name for name, _in, _out in turn.tool_calls]
            print(f"  turn {turn.turn_index}: tools={tools}")

    print("\nMetrics:", aggregate_metrics(records))


if __name__ == "__main__":
    asyncio.run(main())
