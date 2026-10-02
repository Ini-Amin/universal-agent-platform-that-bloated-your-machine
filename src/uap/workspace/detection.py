"""Deterministic Workspace detection (Master sections 2.2, 3 and 68).

The Entry Workflow must *propose* a workspace, never silently switch one
(Master section 68: "never silently switch workspaces"). This module is the
deterministic half of that rule:

* it is pure Python -- no LLM, no I/O, no randomness, no clock;
* it returns a ranked list of candidates with an explicit confidence and a
  human-readable reason, and it never mutates anything;
* below a configurable threshold it returns ``best=None`` and says so, leaving
  the caller (and the user) to decide.

Scoring (documented, deterministic)
-----------------------------------
Both sides are tokenised the same way: lowercase, split on non-alphanumerics,
drop single characters and a small English/Indonesian stopword list.

For a task we build the token set ``T`` from the goal plus the string values in
``input`` (excluding the reserved ``"workspace"`` hint key) and ``constraints``.

For a workspace we build the token set ``W`` from its ``name``, ``description``
and its settings tags (``settings["tags"]`` / ``settings["keywords"]``).

::

    coverage    = |W ∩ T| / |T|          # how much of the task's vocabulary the
                                         # workspace explains
    name_bonus  = 1.0 if the normalised workspace name appears verbatim in the
                       normalised task text, else 0.0
    score       = 0.6 * coverage + 0.4 * name_bonus        # clamped to [0, 1]

A verbatim name mention is a very strong signal, hence the 0.4 bonus; coverage
is the workhorse. With the default threshold of ``0.5`` a workspace must either
explain more than ~83% of the task vocabulary or be named in the task.

Explicit hints
--------------
``task.input["workspace"]`` may carry an exact workspace **id** or **name**. An
explicit hint always wins: it is resolved first, bypasses scoring entirely and
is returned with confidence ``1.0``. An unresolvable hint returns ``best=None``
with a reason saying so -- still no silent switch.

Ambiguity
---------
When the top two scored candidates are within ``ambiguity_margin`` (default
``0.1``) and both clear the threshold, detection refuses to guess: it returns
``best=None``, the tied candidates, and ``reason="ambiguous"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..contracts.models import TaskSpec
from .model import Workspace

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_AMBIGUITY_MARGIN",
    "HINT_KEY",
    "Candidate",
    "DetectionResult",
    "WorkspaceDetector",
]

DEFAULT_THRESHOLD = 0.5
DEFAULT_AMBIGUITY_MARGIN = 0.1
HINT_KEY = "workspace"

# Weights of the two scoring terms. They sum to 1.0 so the score is in [0, 1].
_COVERAGE_WEIGHT = 0.6
_NAME_BONUS_WEIGHT = 0.4

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Deliberately tiny: enough to stop glue words from inflating coverage, small
# enough to stay predictable. Both English and Indonesian, matching the
# bilingual Entry Workflow.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
        "into", "is", "it", "of", "on", "or", "that", "the", "this", "to",
        "with", "my", "our", "your", "i", "we", "you",
        "dan", "di", "ke", "untuk", "yang", "saya", "ini", "itu", "dengan",
    }
)

_SETTINGS_TAG_KEYS = ("tags", "keywords")


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stops and 1-char tokens."""
    return {
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    }


def _normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased text used for verbatim name checks."""
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _as_str_list(value: object) -> list[str]:
    """Coerce a settings tag value into a list of strings, leniently."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value if isinstance(item, str)]
    return []


def _settings_tags(settings: dict[str, object]) -> list[str]:
    tags: list[str] = []
    for key in _SETTINGS_TAG_KEYS:
        tags.extend(_as_str_list(settings.get(key)))
    return tags


def _workspace_tokens(workspace: Workspace) -> set[str]:
    tokens = _tokenize(workspace.name) | _tokenize(workspace.description)
    for tag in _settings_tags(workspace.settings):
        tokens |= _tokenize(tag)
    return tokens


def _task_tokens(task: TaskSpec) -> set[str]:
    tokens = _tokenize(task.goal)
    for key, value in task.input.items():
        if key == HINT_KEY:  # the hint is resolved separately, not scored
            continue
        if isinstance(value, str):
            tokens |= _tokenize(value)
        else:
            for item in _as_str_list(value):
                tokens |= _tokenize(item)
    for value in task.constraints.values():
        if isinstance(value, str):
            tokens |= _tokenize(value)
        else:
            for item in _as_str_list(value):
                tokens |= _tokenize(item)
    return tokens


def _task_text(task: TaskSpec) -> str:
    parts = [task.goal]
    for key, value in task.input.items():
        if key == HINT_KEY:
            continue
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


@dataclass(frozen=True)
class Candidate:
    """One scored workspace, with the evidence that produced its score."""

    workspace: Workspace
    score: float
    matched_terms: tuple[str, ...] = ()
    hint: bool = False


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of one detection pass.

    ``best`` is the workspace the caller should *propose*, or ``None`` when the
    detector refuses to choose (no match, below threshold, or ambiguous).
    ``candidates`` is always the full ranked list so a UI can show the user the
    alternatives (Master section 3: ``[Use Workspace] [Create New]``).
    """

    best: Workspace | None
    candidates: tuple[Candidate, ...]
    confidence: float
    reason: str

    @property
    def ambiguous(self) -> bool:
        return self.reason == "ambiguous"

    @property
    def is_match(self) -> bool:
        return self.best is not None


class WorkspaceDetector:
    """Scores a :class:`TaskSpec` against known workspaces, deterministically."""

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0, 1]")
        if ambiguity_margin < 0.0:
            raise ValueError("ambiguity_margin must be non-negative")
        self.threshold = threshold
        self.ambiguity_margin = ambiguity_margin

    # ------------------------------------------------------------------ #

    def detect(
        self, task: TaskSpec, workspaces: list[Workspace]
    ) -> DetectionResult:
        """Return the ranked detection result for ``task``.

        Never raises for ordinary inputs; an unresolvable or absent hint, a
        below-threshold score, and a tie all yield ``best=None`` with a reason.
        """
        ranked = self._rank(task, workspaces)

        hint = task.input.get(HINT_KEY)
        if hint is not None:
            return self._resolve_hint(str(hint), ranked)

        if not ranked:
            return DetectionResult(
                best=None,
                candidates=(),
                confidence=0.0,
                reason="no workspaces are registered",
            )

        top = ranked[0]
        if top.score < self.threshold:
            return DetectionResult(
                best=None,
                candidates=tuple(ranked),
                confidence=top.score,
                reason=(
                    f"best candidate {top.workspace.name!r} scored "
                    f"{top.score:.2f}, below threshold {self.threshold:.2f}"
                ),
            )

        if len(ranked) > 1 and (top.score - ranked[1].score) <= self.ambiguity_margin:
            return DetectionResult(
                best=None,
                candidates=tuple(ranked),
                confidence=top.score,
                reason="ambiguous",
            )

        return DetectionResult(
            best=top.workspace,
            candidates=tuple(ranked),
            confidence=top.score,
            reason=(
                f"{top.workspace.name!r} matched with score {top.score:.2f}"
            ),
        )

    # ------------------------------------------------------------------ #

    def _rank(self, task: TaskSpec, workspaces: list[Workspace]) -> list[Candidate]:
        """Score every workspace and return candidates best-first."""
        task_tokens = _task_tokens(task)
        task_text = _normalize(_task_text(task))

        candidates: list[Candidate] = []
        for workspace in workspaces:
            tokens = _workspace_tokens(workspace)
            matched = tokens & task_tokens
            coverage = len(matched) / len(task_tokens) if task_tokens else 0.0
            name_bonus = (
                1.0
                if _normalize(workspace.name) and _normalize(workspace.name) in task_text
                else 0.0
            )
            score = min(1.0, _COVERAGE_WEIGHT * coverage + _NAME_BONUS_WEIGHT * name_bonus)
            candidates.append(
                Candidate(
                    workspace=workspace,
                    score=score,
                    matched_terms=tuple(sorted(matched)),
                )
            )

        # Sort by score desc, then name/id asc -- a total order, so results are
        # reproducible regardless of input order.
        candidates.sort(
            key=lambda c: (-c.score, c.workspace.name, c.workspace.id)
        )
        return candidates

    def _resolve_hint(self, hint: str, ranked: list[Candidate]) -> DetectionResult:
        """Resolve an explicit ``task.input["workspace"]`` hint.

        An exact id wins outright; otherwise a case-insensitive name match does.
        The hint always wins when it resolves (Master section 68), and an
        unresolvable hint is reported rather than ignored.
        """
        by_id = [c for c in ranked if c.workspace.id == hint]
        if by_id:
            chosen = by_id[0]
            return DetectionResult(
                best=chosen.workspace,
                candidates=(self._hinted(chosen),),
                confidence=1.0,
                reason=f"explicit workspace hint matched id {hint!r}",
            )

        needle = hint.strip().lower()
        by_name = [c for c in ranked if c.workspace.name.strip().lower() == needle]
        if by_name:
            chosen = min(by_name, key=lambda c: c.workspace.id)
            note = ""
            if len(by_name) > 1:
                note = f" (name matched {len(by_name)} workspaces; chose id {chosen.workspace.id!r})"
            return DetectionResult(
                best=chosen.workspace,
                candidates=tuple(self._hinted(c) for c in by_name),
                confidence=1.0,
                reason=f"explicit workspace hint matched name {hint!r}{note}",
            )

        return DetectionResult(
            best=None,
            candidates=tuple(ranked),
            confidence=0.0,
            reason=(
                f"explicit workspace hint {hint!r} did not match any workspace id or name"
            ),
        )

    @staticmethod
    def _hinted(candidate: Candidate) -> Candidate:
        return Candidate(
            workspace=candidate.workspace,
            score=1.0,
            matched_terms=candidate.matched_terms,
            hint=True,
        )
