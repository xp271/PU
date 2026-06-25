"""
Minimal Agent.run() example.

Run:  set ANTHROPIC_API_KEY=... && python examples/basic_run.py
"""

from __future__ import annotations

import asyncio
import os

from agent_core import Agent, AgentConfig


async def main() -> None:
    # Read API key from environment
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    config = AgentConfig(
        model="anthropic/claude-haiku-4-5",
        api_keys={"anthropic": api_key} if api_key else {},
        max_turns=5,
    )

    agent = Agent(config)
    result = await agent.run("What is the capital of France? Reply in one sentence.")

    print(f"Response: {result.content}")
    print(f"Tokens:   {result.total_tokens}")
    print(f"Tools:    {result.tool_uses}")
    print(f"Stop:     {result.stop_reason}")


if __name__ == "__main__":
    asyncio.run(main())
