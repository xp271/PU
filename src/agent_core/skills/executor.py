"""
Skill executor — inline and fork execution modes.

Both paths delegate to ``run_agent()`` for execution.  The difference is
whether the sub-agent inherits the parent message history or starts fresh.
"""

from __future__ import annotations

import re
from typing import Any

from ..tools.base import ToolCallResult, ToolUseContext
from .definition import SkillDefinition, SkillParameter

# Regex that tolerates optional whitespace inside ``{{ }}`` braces.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(
    template: str,
    arguments: dict[str, str],
    declared: dict[str, SkillParameter],
) -> str:
    """Substitute ``{{ param_name }}`` placeholders in *template*.

    Raises ``ValueError`` when a required declared parameter is missing
    from *arguments*.
    """
    missing = [
        n for n, p in declared.items() if p.required and n not in arguments
    ]
    if missing:
        raise ValueError(
            f"Missing required skill parameters: {', '.join(missing)}"
        )

    def _sub(m: re.Match) -> str:
        return arguments.get(m.group(1), m.group(0))

    return _PLACEHOLDER.sub(_sub, template)


async def execute_skill_inline(
    skill: SkillDefinition,
    arguments: dict[str, str],
    context: ToolUseContext,
) -> ToolCallResult:
    """Execute *skill* as an **inline** sub-agent that inherits the parent
    message history (``parent_messages=context.messages``).

    The child can see the conversation that led to the skill invocation,
    which is useful for skills that need context (e.g. code-review after
    a file was edited).
    """
    from ..agents.run import run_agent
    from ..agents.definition import AgentDefinition

    prompt = render_template(skill.prompt, arguments, skill.parameters)

    definition = AgentDefinition(
        agent_type=f"skill-{skill.name}",
        when_to_use=f"Execute the '{skill.name}' skill.",
        tools=skill.tools,
        system_prompt=skill.system_prompt or f"Execute skill: {skill.name}",
        model=skill.model,
        permission_mode=skill.permission_mode,  # type: ignore[arg-type]
    )

    parts: list[str] = []
    is_error = False

    async for msg in run_agent(
        prompt=prompt,
        definition=definition,
        parent_config=context.parent_config,  # type: ignore[arg-type]
        parent_trace_id=context.parent_trace_id,
        parent_permission_callback=context.parent_permission_callback,
        parent_messages=context.messages,
        parent_system=context.parent_system,
        parent_allowed_tools=context.parent_allowed_tools,
        parent_disallowed_tools=context.parent_disallowed_tools,
        parent_mcp_servers=(
            context.parent_config.mcp_servers  # type: ignore[union-attr]
            if context.parent_config
            else {}
        ),
        parent_context=context,
    ):
        if msg.type == "assistant_text":
            parts.append(msg.content)
        elif msg.type == "done" and msg.stop_reason == "error":
            is_error = True

    return ToolCallResult(content="\n".join(parts), is_error=is_error)


async def execute_skill_fork(
    skill: SkillDefinition,
    arguments: dict[str, str],
    context: ToolUseContext,
) -> ToolCallResult:
    """Execute *skill* as a **fork** sub-agent with a fresh message history
    (no ``parent_messages``).  The child runs in complete isolation.
    """
    from ..agents.run import run_agent
    from ..agents.definition import AgentDefinition

    prompt = render_template(skill.prompt, arguments, skill.parameters)

    definition = AgentDefinition(
        agent_type=f"skill-{skill.name}",
        when_to_use=f"Execute the '{skill.name}' skill.",
        tools=skill.tools,
        system_prompt=skill.system_prompt or f"Execute skill: {skill.name}",
        model=skill.model,
        permission_mode=skill.permission_mode,  # type: ignore[arg-type]
    )

    parts: list[str] = []
    is_error = False

    async for msg in run_agent(
        prompt=prompt,
        definition=definition,
        parent_config=context.parent_config,  # type: ignore[arg-type]
        parent_trace_id=context.parent_trace_id,
        parent_permission_callback=context.parent_permission_callback,
        parent_messages=None,  # fork: no history inheritance
        parent_system=context.parent_system,
        parent_allowed_tools=context.parent_allowed_tools,
        parent_disallowed_tools=context.parent_disallowed_tools,
        parent_mcp_servers=(
            context.parent_config.mcp_servers  # type: ignore[union-attr]
            if context.parent_config
            else {}
        ),
        parent_context=context,
    ):
        if msg.type == "assistant_text":
            parts.append(msg.content)
        elif msg.type == "done" and msg.stop_reason == "error":
            is_error = True

    return ToolCallResult(content="\n".join(parts), is_error=is_error)
