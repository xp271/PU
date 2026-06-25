"""
SkillRegistry — global skill store.

Registered skills are available to ``SkillTool`` and the CLI ``/skills``
command.  Sources: built-in, project (``.agent/skills/``), user
(``~/.agent/skills/``), plugin.
"""

from __future__ import annotations

from .definition import SkillDefinition


class SkillRegistry:
    """Registry of all known skills."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}
        self._sources: dict[str, str] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(
        self, skill: SkillDefinition, source: str = "project"
    ) -> None:
        """Register (or overwrite) a skill.  *source* labels its origin
        (``config`` / ``project`` / ``user`` / ``plugin`` / ``builtin``)."""
        self._skills[skill.name] = skill
        self._sources[skill.name] = source

    def unregister(self, name: str) -> None:
        """Remove a skill by name.  ``KeyError`` if unknown."""
        del self._skills[name]
        self._sources.pop(name, None)

    def get(self, name: str) -> SkillDefinition:
        """Look up a skill by name.  ``KeyError`` if not found."""
        return self._skills[name]

    def source_of(self, name: str) -> str | None:
        """Return the registration source label for *name* (or ``None``)."""
        return self._sources.get(name)

    def contains(self, name: str) -> bool:
        return name in self._skills

    def list_all(self) -> dict[str, SkillDefinition]:
        return dict(self._skills)

    def list_by_source(self, source: str) -> dict[str, SkillDefinition]:
        """Return only the skills registered under *source*."""
        return {
            n: sd
            for n, sd in self._skills.items()
            if self._sources.get(n) == source
        }
