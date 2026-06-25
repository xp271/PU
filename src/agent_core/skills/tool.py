"""
SkillTool — LLM-callable tool for executing skills via tool_use.

Registered as the ``"Skill"`` tool so the LLM can invoke ``/skill-name``
commands as tool-use blocks.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ..tools.base import Tool, ToolCallResult, ToolUseContext
from ..tools.registry import register_tool
from .registry import SkillRegistry


class SkillInput(BaseModel):
    """Validated input for the ``Skill`` tool."""

    skill: str = Field(description="Skill name, e.g. 'review-pr'.")
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description="Key-value pairs passed to the skill template.",
    )
    mode: Literal["inline", "fork"] | None = Field(
        default=None,
        description="Override the skill's default execution mode.",
    )


# Per-query skill registry singleton — set by the query() bootstrap,
# read by SkillTool.call().  Not a global mutable because we want
# per-query isolation.
_SKILL_REGISTRY_KEY = "__skill_registry__"


def _get_skill_registry(context: ToolUseContext) -> SkillRegistry | None:
    return getattr(context, _SKILL_REGISTRY_KEY, None)


def _set_skill_registry(context: ToolUseContext, registry: SkillRegistry) -> None:
    setattr(context, _SKILL_REGISTRY_KEY, registry)


@register_tool
class SkillTool(Tool):
    """Tool that lets the LLM execute named skills.

    Skills are pre-loaded from ``.agent/skills/`` directories (project
    and user level) and optionally contributed by plugins.
    """

    name: ClassVar[str] = "Skill"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    # Must be True — skills run sub-agents which may execute Write/Edit/Bash.
    is_destructive: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Tool interface
    # ------------------------------------------------------------------

    def validate_input(self, input: dict[str, Any]) -> SkillInput:
        return SkillInput.model_validate(input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return SkillInput.model_json_schema()

    async def call(
        self, input: SkillInput, context: ToolUseContext
    ) -> ToolCallResult:
        # -- resolve skill ----------------------------------------------------
        registry = _get_skill_registry(context)
        if registry is None:
            return ToolCallResult(
                content="Skill system is not available (no registry attached).",
                is_error=True,
            )

        try:
            skill = registry.get(input.skill)
        except KeyError:
            return ToolCallResult(
                content=f"Unknown skill: '{input.skill}'",
                is_error=True,
            )

        # -- permission check -------------------------------------------------
        try:
            from .permissions import default_skill_permissions
            engine = default_skill_permissions()
            action = engine.check(skill.name)
            if action == "deny":
                return ToolCallResult(
                    content=f"Skill '{skill.name}' is denied by permission rules.",
                    is_error=True,
                )
            # "ask" → pass through for now (no callback wiring yet);
            # the default rule is "ask" so we allow execution.
        except Exception:
            pass  # permissions are best-effort

        # -- render template --------------------------------------------------
        try:
            from .executor import render_template
            prompt = render_template(skill.prompt, input.arguments, skill.parameters)
        except ValueError as exc:
            return ToolCallResult(content=str(exc), is_error=True)

        # -- execute ----------------------------------------------------------
        mode = input.mode or skill.execution_mode

        try:
            if mode == "fork":
                from .executor import execute_skill_fork
                result = await execute_skill_fork(skill, input.arguments, context)
            else:
                from .executor import execute_skill_inline
                result = await execute_skill_inline(skill, input.arguments, context)
        except Exception as exc:
            return ToolCallResult(
                content=f"Skill execution error: {exc}", is_error=True
            )

        return result
