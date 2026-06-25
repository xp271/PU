"""
Agent.stream() example — consumes typed messages as they arrive.

Run:  set ANTHROPIC_API_KEY=... && python examples/streaming.py
"""

from __future__ import annotations

import asyncio
import os

from agent_core import Agent, AgentConfig, AssistantText, ToolUse, ToolResult


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    config = AgentConfig(
        model="anthropic/claude-haiku-4-5",
        api_keys={"anthropic": api_key} if api_key else {},
        max_turns=5,
        permission_mode="bypassPermissions",
    )

    agent = Agent(config)

    print("Streaming...\n")
    async for msg in agent.stream("List files in the current directory"):
        match msg:
            case AssistantText(content=text):
                print(text, end="", flush=True)
            case ToolUse(tool_name=name, tool_input=inp):
                print(f"\n[Tool: {name}({inp})]")
            case ToolResult(tool_name=name, is_error=True):
                print(f"\n[Error from {name}]")
            case ToolResult(tool_name=name):
                print(f"\n[{name} done]")


if __name__ == "__main__":
    asyncio.run(main())
