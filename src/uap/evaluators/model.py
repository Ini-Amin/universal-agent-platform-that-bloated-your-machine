"""Versioned evaluator ENTITIES and evaluation records (Master section 47).

The existing :mod:`uap.evaluation` harness runs a fixed set of platform metrics.
This module adds *versioned evaluator entities*: a named, criteria-driven
:class:`EvaluatorSpec` that can be re-registered to produce new versions, and an
immutable :class:`EvaluationRecord` of one evaluator run against one subject.

Everything is deterministic and offline (section 47: "Evaluations themselves
must be versioned and reproducible").
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts.models import UTCDateTime, VerificationResult, utc_now

__all__ = ["EvaluatorSpec", "EvaluationRecord"]

_FORBID = ConfigDict(extra="forbid")


class EvaluatorSpec(BaseModel):
    """One immutable, versioned evaluator definition.

    ``criteria`` is a list of ``{"name", "kind", "params"}`` dicts. ``kind`` is
    one of ``min_count``, ``non_empty``, ``has_field``, ``regex_match`` or
    ``threshold``; ``params`` carries the per-kind arguments (see
    :class:`~uap.evaluators.runner.EvaluatorRunner`).
    """

    model_config = _FORBID

    evaluator_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    version: int = 1
    description: str = ""
    criteria: list[dict]
    target_kind: str = "artifact"
    schema_version: int = 1

    @property
    def ref(self) -> str:
        """The exact ``name@vN`` reference for this evaluator version."""
        return f"{self.name}@v{self.version}"


class EvaluationRecord(BaseModel):
    """The immutable result of running one evaluator against one subject."""

    model_config = _FORBID

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    evaluator_ref: str
    subject_ref: str
    result: VerificationResult
    metrics: dict = Field(default_factory=dict)
    created_at: UTCDateTime = Field(default_factory=utc_now)
