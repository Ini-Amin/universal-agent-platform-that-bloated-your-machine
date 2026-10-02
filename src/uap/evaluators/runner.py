"""Evaluator registry + runner (Master section 47).

The registry holds evaluator SPECS keyed by name, versioning each ``add`` call
into ``v(n+1)``. The runner interprets a spec's criteria against a plain
``subject`` dict and returns an :class:`EvaluationRecord` wrapping a
deterministic :class:`~uap.contracts.models.VerificationResult`.

Criterion kinds (``params`` keys):

* ``min_count``   -- ``field``, ``n``: ``len(subject[field]) >= n``
* ``non_empty``   -- ``field``: value is a non-blank str / non-empty collection
* ``has_field``   -- ``field``: key present (any value, incl. ``None``)
* ``regex_match`` -- ``field``, ``pattern``: ``re.search(pattern, str(value))``
* ``threshold``   -- ``field``, ``op`` (``ge``/``gt``/``le``/``lt``/``eq``/``ne``),
  ``value``: numeric comparison

A missing field never raises: the criterion fails with evidence
``"missing field <name>"``.
"""

from __future__ import annotations

import operator
import re
from typing import Callable

from uap.contracts.models import VerificationCriterion, VerificationResult

from .model import EvaluationRecord, EvaluatorSpec

__all__ = ["EvaluatorRegistry", "EvaluatorRunner"]

_MISSING = object()

_OPS: dict[str, Callable[[object, object], bool]] = {
    "ge": operator.ge,
    "gt": operator.gt,
    "le": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
    "ne": operator.ne,
}


class EvaluatorRegistry:
    """In-memory registry of evaluator specs, versioned per name."""

    def __init__(self) -> None:
        # name -> ordered list of specs (index 0 == v1).
        self._by_name: dict[str, list[EvaluatorSpec]] = {}

    def add(
        self,
        name: str,
        criteria: list[dict],
        *,
        target_kind: str = "artifact",
        description: str = "",
    ) -> EvaluatorSpec:
        """Register ``name`` as a new version (``v(n+1)``) and return its spec."""
        if not name or not str(name).strip():
            raise ValueError("evaluator name must be a non-empty string")
        if not isinstance(criteria, list):
            raise ValueError("criteria must be a list of dicts")
        version = len(self._by_name.get(name, [])) + 1
        spec = EvaluatorSpec(
            name=name,
            version=version,
            description=description,
            criteria=[dict(c) for c in criteria],
            target_kind=target_kind,
        )
        self._by_name.setdefault(name, []).append(spec)
        return spec

    def resolve_ref(self, ref: str) -> EvaluatorSpec:
        """Resolve an exact ``name@vN`` reference, else the latest by bare name."""
        if "@v" in ref:
            name, _, raw = ref.partition("@v")
            versions = self._by_name.get(name)
            try:
                wanted = int(raw)
            except ValueError:
                wanted = -1
            if versions:
                for spec in versions:
                    if spec.version == wanted:
                        return spec
            raise KeyError(
                f"no evaluator {ref!r}; available: {sorted(self._refs())}"
            )
        if ref in self._by_name and self._by_name[ref]:
            return self._by_name[ref][-1]
        raise KeyError(f"no evaluator {ref!r}; available: {sorted(self._refs())}")

    def latest(self, name: str) -> EvaluatorSpec:
        """Return the newest version registered under ``name``."""
        versions = self._by_name.get(name)
        if not versions:
            raise KeyError(f"no evaluator {name!r}; available: {sorted(self._refs())}")
        return versions[-1]

    def all(self) -> list[EvaluatorSpec]:
        """Every registered spec, every version, ordered by name then version."""
        out: list[EvaluatorSpec] = []
        for name in sorted(self._by_name):
            out.extend(self._by_name[name])
        return out

    def _refs(self) -> list[str]:
        return [spec.ref for specs in self._by_name.values() for spec in specs]


class EvaluatorRunner:
    """Run evaluator specs against plain subject dicts, deterministically."""

    def __init__(self, registry: EvaluatorRegistry) -> None:
        self.registry = registry

    def run(self, ref: str, subject: dict) -> EvaluationRecord:
        """Run the evaluator resolved from ``ref`` against ``subject``."""
        spec = self.registry.resolve_ref(ref)
        exact_ref = spec.ref
        criteria = [self._run_criterion(c, subject) for c in spec.criteria]
        passed = all(c.passed for c in criteria)
        total = len(criteria)
        score = (sum(1 for c in criteria if c.passed) / total) if total else 1.0
        result = VerificationResult(
            verifier=exact_ref,
            passed=passed,
            criteria=criteria,
            score=score,
        )
        return EvaluationRecord(
            evaluator_ref=exact_ref,
            subject_ref=self._subject_ref(subject),
            result=result,
        )

    def run_all(
        self, subject: dict, *, target_kind: str | None = None
    ) -> list[EvaluationRecord]:
        """Run the latest version of every evaluator (optionally filtered)."""
        records: list[EvaluationRecord] = []
        for name in sorted(self.registry._by_name):
            spec = self.registry.latest(name)
            if target_kind is not None and spec.target_kind != target_kind:
                continue
            records.append(self.run(spec.ref, subject))
        return records

    # ------------------------------------------------------------------ #
    # Criterion evaluation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _subject_ref(subject: dict) -> str:
        for key in ("execution_id", "artifact_id", "id", "subject_ref"):
            value = subject.get(key)
            if value:
                return str(value)
        return "unknown"

    def _run_criterion(
        self, criterion: dict, subject: dict
    ) -> VerificationCriterion:
        name = str(criterion.get("name", criterion.get("kind", "criterion")))
        kind = str(criterion.get("kind", ""))
        params = criterion.get("params", {}) or {}
        handler = getattr(self, f"_kind_{kind}", None)
        if handler is None:
            return VerificationCriterion(
                criterion=name,
                passed=False,
                evidence=f"unknown criterion kind {kind!r}",
            )
        field = params.get("field")
        value = subject.get(field, _MISSING) if field is not None else _MISSING
        if field is not None and value is _MISSING:
            return VerificationCriterion(
                criterion=name,
                passed=False,
                evidence=f"missing field {field}",
            )
        return handler(name, value, params)

    def _kind_min_count(self, name, value, params) -> VerificationCriterion:
        n = int(params.get("n", 1))
        try:
            count = len(value)
        except TypeError:
            return VerificationCriterion(
                criterion=name, passed=False, evidence=f"{value!r} has no length"
            )
        return VerificationCriterion(
            criterion=name,
            passed=count >= n,
            evidence=f"count={count} (need >= {n})",
        )

    def _kind_non_empty(self, name, value, params) -> VerificationCriterion:
        if isinstance(value, str):
            empty = not value.strip()
        elif value is None:
            empty = True
        else:
            try:
                empty = len(value) == 0
            except TypeError:
                empty = False
        return VerificationCriterion(
            criterion=name,
            passed=not empty,
            evidence="empty" if empty else "non-empty",
        )

    def _kind_has_field(self, name, value, params) -> VerificationCriterion:
        # The field existed (missing was handled upstream), so this passes.
        return VerificationCriterion(
            criterion=name, passed=True, evidence=f"field {params.get('field')} present"
        )

    def _kind_regex_match(self, name, value, params) -> VerificationCriterion:
        pattern = str(params.get("pattern", ""))
        matched = re.search(pattern, str(value)) is not None
        return VerificationCriterion(
            criterion=name,
            passed=matched,
            evidence=f"{'matched' if matched else 'no match'} /{pattern}/",
        )

    def _kind_threshold(self, name, value, params) -> VerificationCriterion:
        op_name = str(params.get("op", "ge"))
        op = _OPS.get(op_name)
        if op is None:
            return VerificationCriterion(
                criterion=name, passed=False, evidence=f"unknown op {op_name!r}"
            )
        threshold = params.get("value")
        try:
            ok = op(float(value), float(threshold))
        except (TypeError, ValueError):
            return VerificationCriterion(
                criterion=name,
                passed=False,
                evidence=f"non-numeric comparison {value!r} {op_name} {threshold!r}",
            )
        return VerificationCriterion(
            criterion=name,
            passed=bool(ok),
            evidence=f"{value} {op_name} {threshold} -> {bool(ok)}",
        )
