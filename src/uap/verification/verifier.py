"""Deterministic, criteria-based verification (Master section 17).

Verification is explicit and evidence-backed: every check is a named
``Criterion`` whose ``check`` callable returns pass/fail. A verifier never asks
an LLM "does this look right?" -- it runs deterministic predicates and records
the evidence for each one (Master section 29 rule 11: prefer deterministic
logic wherever deterministic logic is sufficient).

A criterion whose ``check`` raises is *never* allowed to break the run: the
exception is captured as a failed criterion whose evidence is the error text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from uap.contracts.models import VerificationCriterion, VerificationResult

__all__ = [
    "Criterion",
    "Verifier",
    "DeterministicVerifier",
    "has_min_evidence",
    "all_sources_have_urls",
    "synthesis_non_empty",
]

@dataclass(frozen=True)
class Criterion:
    """One explicit, measurable check applied to a subject.

    ``check`` maps the subject to a boolean verdict. ``evidence`` is an optional
    callable that renders a human-readable justification for the criterion.
    """

    name: str
    check: Callable[[object], bool]
    evidence: Callable[[object], str] | None = None

@runtime_checkable
class Verifier(Protocol):
    """Structural interface every verifier satisfies (deterministic or not)."""

    name: str

    def verify(self, subject: object) -> VerificationResult: ...

def _render_evidence(crit: Criterion, subject: object) -> str | None:
    """Best-effort evidence rendering; a failing evidence hook is contained."""
    if crit.evidence is None:
        return None
    try:
        rendered = crit.evidence(subject)
    except Exception as exc:  # noqa: BLE001 - evidence must never break a run
        return f"evidence error: {type(exc).__name__}: {exc}"
    if rendered is None:
        return None
    text = str(rendered)
    return text if text else None

def _run_criteria(
    criteria: list[Criterion], subject: object
) -> list[VerificationCriterion]:
    """Run every criterion, converting outcomes (and exceptions) to records."""
    results: list[VerificationCriterion] = []
    for crit in criteria:
        try:
            passed = bool(crit.check(subject))
        except Exception as exc:  # noqa: BLE001 - a raising check is a failure
            results.append(
                VerificationCriterion(
                    criterion=crit.name,
                    passed=False,
                    evidence=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        evidence = _render_evidence(crit, subject)
        if not passed and evidence is None:
            evidence = "criterion returned False"
        results.append(
            VerificationCriterion(
                criterion=crit.name, passed=passed, evidence=evidence
            )
        )
    return results

def _score(results: list[VerificationCriterion]) -> float:
    total = len(results)
    if total == 0:
        # Vacuous truth: nothing to check means nothing failed.
        return 1.0
    passed = sum(1 for item in results if item.passed)
    return passed / total

class DeterministicVerifier:
    """Runs a fixed list of criteria and returns a scored VerificationResult."""

    def __init__(self, name: str, criteria: list[Criterion]) -> None:
        self.name = name
        self.criteria = list(criteria)

    def verify(self, subject: object) -> VerificationResult:
        """Run every criterion against ``subject``. Never raises."""
        results = _run_criteria(self.criteria, subject)
        return VerificationResult(
            verifier=self.name,
            passed=all(item.passed for item in results),
            criteria=results,
            score=_score(results),
        )

# --------------------------------------------------------------------------- #
# Ready-made criteria factories for research outputs (Master section 7).
#
# They are duck-typed: the subject may be a mapping or an object exposing the
# named attributes, so the verification layer stays decoupled from whichever
# concrete research-output model a workflow ships.
# --------------------------------------------------------------------------- #

def _field(subject: object, *names: str) -> object | None:
    for name in names:
        if isinstance(subject, dict):
            if name in subject:
                return subject[name]
        elif hasattr(subject, name):
            return getattr(subject, name)
    return None

def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []

def _source_url(source: object) -> object | None:
    return _field(source, "url", "uri", "link", "source_url")

def has_min_evidence(minimum: int) -> Criterion:
    """Pass when the output carries at least ``minimum`` evidence items."""
    if minimum < 0:
        raise ValueError("minimum must be >= 0")

    def check(subject: object) -> bool:
        return len(_as_list(_field(subject, "evidence", "evidence_items"))) >= minimum

    def evidence(subject: object) -> str:
        count = len(_as_list(_field(subject, "evidence", "evidence_items")))
        return f"{count} evidence item(s), minimum {minimum}"

    return Criterion(
        name=f"has_min_evidence({minimum})", check=check, evidence=evidence
    )

def all_sources_have_urls() -> Criterion:
    """Pass when every cited source exposes a non-empty URL."""

    def _missing(subject: object) -> list[str]:
        missing: list[str] = []
        for index, source in enumerate(_as_list(_field(subject, "sources"))):
            url = _source_url(source)
            if not isinstance(url, str) or not url.strip():
                missing.append(str(index))
        return missing

    def check(subject: object) -> bool:
        return not _missing(subject)

    def evidence(subject: object) -> str:
        missing = _missing(subject)
        if missing:
            return f"sources missing url at index: {', '.join(missing)}"
        return "every source has a url"

    return Criterion(
        name="all_sources_have_urls", check=check, evidence=evidence
    )

def synthesis_non_empty() -> Criterion:
    """Pass when the synthesis is a non-blank string."""

    def check(subject: object) -> bool:
        value = _field(subject, "synthesis", "synthesis_text", "summary")
        return isinstance(value, str) and bool(value.strip())

    def evidence(subject: object) -> str:
        value = _field(subject, "synthesis", "synthesis_text", "summary")
        if not isinstance(value, str):
            return f"synthesis is {type(value).__name__}, expected str"
        return f"synthesis length {len(value.strip())}"

    return Criterion(
        name="synthesis_non_empty", check=check, evidence=evidence
    )
