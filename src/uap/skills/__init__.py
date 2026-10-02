"""Skill System (Master section 11).

A Skill is reusable *methodology*: it describes HOW to perform a type of task.
A Skill is not a Tool -- tools are actions, skills are methodology, composed as
``Agent -> Skill -> Tools``. Skills are therefore plain, serializable data that
can be authored as Markdown and loaded without a YAML dependency.
"""

from __future__ import annotations

from uap.skills.loader import (
    FrontmatterError,
    load_skill_file,
    load_skills_from_dir,
    load_skills_lenient,
    parse_frontmatter,
)
from uap.skills.registry import SkillRegistry
from uap.skills.skill import Skill

__all__ = [
    "FrontmatterError",
    "Skill",
    "SkillRegistry",
    "load_skill_file",
    "load_skills_from_dir",
    "load_skills_lenient",
    "parse_frontmatter",
]
