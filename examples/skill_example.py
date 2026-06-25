"""
Item 9 example: define a skill on disk, register a hook, and run an Agent.

This shows the three Item-9 subsystems working together:

* **Skills** — a ``SKILL.md`` discovered from ``skills_dir`` and invoked via
  the built-in ``Skill`` tool.
* **Hooks** — a ``pre_tool_use`` hook attached through ``AgentConfig.hooks``.
* **Plugins** — see ``agent_core.plugins`` (entry-point group
  ``agent_core.plugins``); auto-enable them via ``AgentConfig.auto_enable_plugins``.

Run:  ``python examples/skill_example.py``  (needs a real ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agent_core import Agent, AgentConfig
from agent_core.hooks.types import HookContinue


# --------------------------------------------------------------------------
# A trivial audit hook — logs every tool call, never blocks.
# --------------------------------------------------------------------------
class AuditHook:
    hook_kind = "pre_tool_use"

    async def __call__(self, tool_name, tool_input, context):
        print(f"[audit] about to run tool: {tool_name}")
        return HookContinue()


SKILL_BODY = """---
name: summarize
description: Summarize a topic in three bullet points
parameters:
  topic:
    type: string
    description: What to summarize
    required: true
---

Summarize **{{ topic }}** in exactly three concise bullet points.
"""


def _write_skill(root: Path) -> None:
    skill_dir = root / "summarize"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_skill(Path(tmp))

        config = AgentConfig(
            model="anthropic/claude-sonnet-4-5",
            permission_mode="bypassPermissions",
            skills_dir=tmp,            # discover the skill above
            hooks=[AuditHook()],       # attach the audit hook
            # auto_enable_plugins=["my-plugin"],  # if you ship a plugin
        )

        agent = Agent(config)
        result = await agent.run(
            "Use the 'summarize' skill with topic='the water cycle'."
        )
        print("\n=== FINAL ===")
        print(result.content)
        print(f"(stop_reason={result.stop_reason}, tokens={result.total_tokens})")


if __name__ == "__main__":
    asyncio.run(main())
