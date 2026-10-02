"""In-memory Skill registry (Master section 11).

The registry is a pure lookup structure: it stores skills by name and answers
deterministic queries. It knows nothing about tools, agents, workflows, or the
filesystem -- loading skills from disk is the loader's job.
"""

from __future__ import annotations

from uap.skills.skill import Skill

__all__ = ["SkillRegistry"]


class SkillRegistry:
    """Stores :class:`Skill` objects and answers deterministic queries.

    Every listing method sorts by skill name, so results never depend on the
    order in which skills were registered.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Register ``skill``. Duplicate names are an error, not an overwrite."""
        if skill.name in self._skills:
            raise ValueError(f"Skill {skill.name!r} is already registered")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        """Return the skill named ``name``, or ``None`` when unknown."""
        return self._skills.get(name)

    def find_by_domain(self, domain: str) -> list[Skill]:
        """All skills whose domain equals ``domain``, ordered by name."""
        return self._sorted(s for s in self._skills.values() if s.domain == domain)

    def find_by_capability(self, capability: str) -> list[Skill]:
        """All skills that assume ``capability``, ordered by name."""
        return self._sorted(
            s for s in self._skills.values() if capability in s.required_capabilities
        )

    def names(self) -> list[str]:
        """Every registered skill name, ordered ascending."""
        return sorted(self._skills)

    @staticmethod
    def _sorted(skills: object) -> list[Skill]:
        return sorted(skills, key=lambda skill: skill.name)  # type: ignore[arg-type]
