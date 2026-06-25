"""
Skill loader — parse ``SKILL.md`` frontmatter and discover skill directories.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import frontmatter

from .definition import SkillDefinition, SkillParameter

logger = logging.getLogger("agent_core.skills")


def load_skill_from_markdown(file_path: str | Path) -> SkillDefinition | None:
    """Parse a single ``SKILL.md`` file into a ``SkillDefinition``.

    Returns ``None`` when parsing fails (frontmatter is missing required
    fields or is syntactically invalid).
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        return None

    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:
        logger.warning("Failed to parse '%s': %s", file_path, exc)
        return None

    meta: dict = post.metadata
    if not meta:
        return None

    name = meta.get("name")
    if not name or not isinstance(name, str):
        logger.debug("Skipping '%s': no 'name' in frontmatter", file_path)
        return None

    # Build SkillDefinition
    parameters: dict[str, SkillParameter] = {}
    raw_params = meta.get("parameters", {})
    if isinstance(raw_params, dict):
        for p_name, p_def in raw_params.items():
            if isinstance(p_def, dict):
                parameters[p_name] = SkillParameter(
                    type=p_def.get("type", "string"),
                    description=p_def.get("description", ""),
                    required=p_def.get("required", False),
                )

    return SkillDefinition(
        name=name,
        description=meta.get("description", ""),
        tools=meta.get("tools", ["*"]),
        model=meta.get("model"),
        permission_mode=meta.get("permission_mode"),
        system_prompt=meta.get("system_prompt", ""),
        prompt=post.content or "",
        parameters=parameters,
        execution_mode=meta.get("execution_mode", "inline"),
        background=meta.get("background", False),
    )


def discover_skills(directory: str | Path) -> dict[str, SkillDefinition]:
    """Scan *directory* for subdirectories containing a ``SKILL.md`` file.

    Convention (aligned with Claude Code):
        ``<directory>/<skill-name>/SKILL.md``

    Returns a ``{name: SkillDefinition}`` dict.  Name conflicts are
    resolved first-come-first-served with a warning.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}

    result: dict[str, SkillDefinition] = {}

    for entry in sorted(os.listdir(directory)):
        skill_dir = directory / entry
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        sd = load_skill_from_markdown(skill_file)
        if sd is None:
            continue
        if sd.name in result:
            logger.warning(
                "Skill name '%s' already registered from a previous "
                "directory — keeping the first encountered.",
                sd.name,
            )
            continue
        result[sd.name] = sd

    return result
