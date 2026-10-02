"""The Skill contract (Master section 11).

A Skill is reusable *methodology*: it describes HOW to perform a type of task.
It is deliberately not a Tool -- tools are actions, skills are methodology, and
an agent composes them as ``Agent -> Skill -> Tools`` (Master section 11).

The ``required_capabilities`` list names the *tool capabilities* the methodology
assumes (e.g. ``"http.request"``); it never names concrete tools, so a skill
stays independent of any particular Tool Registry implementation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Skill"]


class Skill(BaseModel):
    """A reusable methodology for a type of task (Master section 11)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    domain: str = ""
    methodology: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    version: int = 1
