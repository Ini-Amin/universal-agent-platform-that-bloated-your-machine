"""Verification + review public API (Master sections 17 and 18).

Step 12: every important operation is checked against explicit, measurable
criteria before it is treated as done, and domain results pass through a
checklist reviewer before they become final.
"""

from uap.verification.reviewer import ReviewDecision, Reviewer
from uap.verification.verifier import (
    Criterion,
    DeterministicVerifier,
    Verifier,
    all_sources_have_urls,
    has_min_evidence,
    synthesis_non_empty,
)

__all__ = [
    "Criterion",
    "Verifier",
    "DeterministicVerifier",
    "has_min_evidence",
    "all_sources_have_urls",
    "synthesis_non_empty",
    "ReviewDecision",
    "Reviewer",
]
