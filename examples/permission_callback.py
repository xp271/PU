"""
Permission callback injection example.

The callback is consulted for every tool call. Return True to allow,
False to deny.

Run:  set ANTHROPIC_API_KEY=... && python examples/permission_callback.py
"""

from __future__ import annotations

import asyncio
import os

from agent_core import Agent, AgentConfig


async def my_permission(tool_name: str, tool_input: dict) -> bool:
    """Approve read-only tools, deny destructive ones."""
    read_only = {"Read", "Glob", "Grep"}
    if tool_name in read_only:
        print(f"  [ALLOW] {tool_name}")
        return True
    print(f"  [DENY]  {tool_name}")
    return False


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    config = AgentConfig(
        model="anthropic/claude-haiku-4-5",
        api_keys={"anthropic": api_key} if api_key else {},
        max_turns=5,
        permission_mode="ask",  # consult callback for every tool
    )

    agent = Agent(config)
    agent.on_permission_check(my_permission)

    result = await agent.run(
        "Read README.md and write a one-line summary to /tmp/summary.txt"
    )

    print(f"\nFinal: {result.stop_reason} | tokens={result.total_tokens}")
    print(f"Result: {result.content[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
