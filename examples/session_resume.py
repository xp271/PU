"""
Session persistence and resume example.

Demonstrates: new session → persist → list → resume with full history.

Run:  set ANTHROPIC_API_KEY=... && python examples/session_resume.py

Note: list_sessions / load_session are currently internal modules
(agent_core.persistence.session). If these APIs stabilise they may
be promoted to the top-level agent_core namespace.
"""

from __future__ import annotations

import asyncio
import os

from agent_core import Agent, AgentConfig
from agent_core.session import SessionState
from agent_core.persistence.path import compute_project_hash, session_jsonl_path
from agent_core.persistence.session import list_sessions, load_session


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    config = AgentConfig(
        model="anthropic/claude-haiku-4-5",
        api_keys={"anthropic": api_key} if api_key else {},
        max_turns=5,
    )

    ph = compute_project_hash(config.workspace_root)

    # ---- 1. First run: create a session and persist it ----
    session = SessionState()
    sid = session.session_id
    await session.begin_persistence(session_jsonl_path(ph, sid))

    agent = Agent(config, session=session)
    result = await agent.run("My name is Ada. Remember it and reply 'OK'.")
    print(f"Run 1: {result.content.strip()}")
    print(f"  session_id = {sid}")

    await session.end_persistence()

    # ---- 2. List saved sessions ----
    print("\nSaved sessions:")
    for info in await list_sessions(config.workspace_root):
        print(f"  {info.session_id}  messages={info.message_count}  {info.last_activity}")

    # ---- 3. Resume: load the session and continue ----
    restored = await load_session(sid, workspace_root=config.workspace_root)
    if restored is None:
        print("ERROR: failed to load session")
        return

    agent2 = Agent(config, session=restored)
    result2 = await agent2.run("What is my name? Reply in one sentence.")
    print(f"\nRun 2 (resumed): {result2.content.strip()}")
    # Should mention "Ada"


if __name__ == "__main__":
    asyncio.run(main())
