"""
Skills system — named, reusable prompt templates with parameter expansion.

Public API
----------
- ``SkillDefinition`` / ``SkillParameter`` — data models.
- ``SkillRegistry`` — skill store.
- ``load_skill_from_markdown`` / ``discover_skills`` — loader.
- ``render_template`` / ``execute_skill_inline`` / ``execute_skill_fork`` — execution.
- ``SkillTool`` — LLM-callable tool (already registered via ``@register_tool``).
- ``SkillPermissionEngine`` / ``default_skill_permissions`` — permissions.
- ``_set_skill_registry`` / ``_get_skill_registry`` — per-query registry plumbing.
"""

from __future__ import annotations

from .definition import SkillDefinition, SkillParameter
from .executor import execute_skill_fork, execute_skill_inline, render_template
from .loader import discover_skills, load_skill_from_markdown
from .permissions import (
    SkillPermissionEngine,
    SkillPermissionRule,
    default_skill_permissions,
)
from .registry import SkillRegistry
from .tool import SkillTool, _get_skill_registry, _set_skill_registry

__all__ = [
    "SkillDefinition",
    "SkillParameter",
    "SkillRegistry",
    "SkillTool",
    "SkillPermissionEngine",
    "SkillPermissionRule",
    "default_skill_permissions",
    "load_skill_from_markdown",
    "discover_skills",
    "render_template",
    "execute_skill_inline",
    "execute_skill_fork",
    "_get_skill_registry",
    "_set_skill_registry",
]
