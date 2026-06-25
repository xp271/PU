"""
SkillDefinition — a reusable prompt template with metadata.

Skills are defined in ``SKILL.md`` files (one per directory) with
YAML frontmatter for metadata and Markdown body as the prompt template.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SkillParameter(BaseModel):
    """Metadata for a single skill parameter."""

    type: str = "string"  # string | int | bool (for documentation only)
    description: str = ""
    required: bool = False


class SkillDefinition(BaseModel):
    """Definition of a reusable skill / workflow.

    The ``prompt`` field holds the rendered body of the ``SKILL.md`` file
    (the template with ``{{ param }}`` placeholders).
    """

    name: str
    """Unique skill identifier (e.g. ``"review-pr"``)."""

    description: str = ""
    """Short description shown to the LLM for routing decisions."""

    tools: list[str] = ["*"]
    """Tool white-list for the skill execution environment."""

    model: str | None = None
    """Optional model override.  ``None`` inherits the parent model."""

    permission_mode: str | None = None
    """Optional permission-mode override (clamped by parent)."""

    system_prompt: str = ""
    """Additional system prompt injected into the skill's execution."""

    prompt: str = ""
    """The resolved body (template) of the ``SKILL.md`` file."""

    parameters: dict[str, SkillParameter] = Field(default_factory=dict)
    """Declared parameters with types and required flags."""

    execution_mode: Literal["inline", "fork"] = "inline"
    """Default execution strategy (can be overridden by the caller)."""

    background: bool = False
    """When ``True`` and ``execution_mode="fork"``, run as an async
    background sub-agent (same semantics as ``AgentTool``)."""
