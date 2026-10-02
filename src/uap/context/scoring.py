"""Deterministic relevance scoring for the Context Compiler (Master section 15).

Pure functions only: no IO, no network, no clock reads. Given the same inputs
this module always returns the same candidate list, which is what lets the
compiler honour "select only relevant context" instead of dumping every memory
row, knowledge item, and user observation into the prompt (Master section 29,
rule 5).

Scoring policy (one line):

    score = keyword_overlap * source_weight (+ tiny recency bonus for memory)

where ``keyword_overlap`` is the number of distinct query tokens (from
``task.goal`` plus ``task.input`` values) that also appear in the candidate text,
and ``source_weight`` is one of:

    task_state -> 1.0
    memory     -> 0.8 * memory.relevance
    user_model -> 0.6
    knowledge  -> 0.5 + provided score
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from uap.contracts.models import Memory, TaskSpec, UserModel, WorkflowState

__all__ = [
    "STOPWORDS",
    "WEIGHT_PRIOR_OUTPUT",
    "Candidate",
    "tokenize",
    "query_tokens",
    "overlap_count",
    "build_candidates",
    "filter_secrets",
    "task_state_content",
    "user_model_content",
]

# --------------------------------------------------------------------------- #
# Source weights (Master section 15 ranking policy)
# --------------------------------------------------------------------------- #

WEIGHT_TASK_STATE = 1.0
WEIGHT_MEMORY = 0.8
WEIGHT_USER_MODEL = 0.6
WEIGHT_PRIOR_OUTPUT = 0.9
WEIGHT_KNOWLEDGE = 0.5
# Recency only breaks ties; it must never outrank a real relevance difference.
_RECENCY_EPS = 1e-6

_WORD_RE = re.compile(r"[a-z0-9]+")

# Small English + Indonesian stopword set. Words shorter than three characters
# are dropped by the tokenizer before this set is even consulted.
STOPWORDS = frozenset(
    {
        # English
        "the", "and", "for", "are", "was", "were", "with", "from", "this",
        "that", "these", "those", "you", "your", "our", "its", "his", "her",
        "they", "them", "then", "than", "there", "here", "when", "where",
        "which", "while", "who", "whom", "what", "why", "how", "can", "could",
        "should", "would", "will", "shall", "may", "might", "must", "not",
        "but", "any", "all", "both", "each", "few", "more", "most", "other",
        "some", "such", "only", "own", "same", "too", "very", "into", "over",
        "under", "about", "after", "before", "between", "out", "off", "via",
        "per", "use", "used", "using", "one", "two", "also", "get", "got",
        "make", "made", "new", "old", "way",
        # Indonesian
        "yang", "dan", "untuk", "dengan", "adalah", "pada", "dari", "ini",
        "itu", "atau", "juga", "akan", "tidak", "bukan", "saya", "kamu",
        "kita", "kami", "mereka", "dia", "ada", "sudah", "belum", "bisa",
        "harus", "karena", "agar", "supaya", "dalam", "oleh", "sebagai",
        "secara", "bahwa", "para", "satu", "dua", "hal", "tapi", "tetapi",
        "jika", "kalau", "saat", "ketika", "setelah", "sebelum",
    }
)


# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #


def tokenize(text: Any) -> list[str]:
    """Lowercase alphanumeric tokens, dropping short words and stopwords."""
    if text is None:
        return []
    words = _WORD_RE.findall(str(text).lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def query_tokens(task: TaskSpec) -> frozenset[str]:
    """Tokens describing what the task is about (goal + input values)."""
    parts: list[str] = [task.goal or ""]
    for value in (task.input or {}).values():
        parts.append(_stringify(value))
    tokens: set[str] = set()
    for part in parts:
        tokens.update(tokenize(part))
    return frozenset(tokens)


def overlap_count(query: frozenset[str], text: Any) -> int:
    """How many distinct query tokens appear in ``text``."""
    if not query:
        return 0
    return len(query & set(tokenize(text)))


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """One scored context candidate, ready for the compiler to rank."""

    key: str
    content: str
    source: str
    score: float
    source_ref: str = ""


_SENSITIVE_KEYS = frozenset(
    {"secret", "token", "password", "credential", "api_key", "auth", "privkey"}
)


def _is_sensitive_key(key: Any) -> bool:
    k = str(key).lower()
    return any(s in k for s in _SENSITIVE_KEYS)


def filter_secrets(data: Any) -> Any:
    """Recursively scrub sensitive keys matching credentials/tokens/secrets."""
    if isinstance(data, dict):
        return {
            k: filter_secrets(v)
            for k, v in data.items()
            if not _is_sensitive_key(k)
        }
    if isinstance(data, list):
        return [filter_secrets(item) for item in data]
    return data

def task_state_content(state: WorkflowState) -> str:
    """Render the workflow state as deterministic, agent-readable text."""
    status = getattr(state.status, "value", state.status)
    parts = [
        f"workflow={state.workflow}",
        f"status={status}",
        f"current_node={state.current_node or ''}",
        f"retries={state.retries}",
    ]
    if state.node_history:
        parts.append("node_history=" + " -> ".join(state.node_history))
    if state.data:
        clean = filter_secrets(state.data)
        parts.append("data=" + json.dumps(clean, sort_keys=True, default=str))
    return "\n".join(parts)


def user_model_content(user_model: UserModel) -> str:
    """Render preferences, skill levels, and observations as text.

    Observation notes are included verbatim so a relevant observation is both
    scored and visible to the agent (Master section 16).
    """
    parts: list[str] = [f"user_id={user_model.user_id}"]
    if user_model.preferences:
        parts.append(
            "preferences="
            + json.dumps(user_model.preferences, sort_keys=True, default=str)
        )
    if user_model.domain_skill_levels:
        parts.append(
            "skill_levels="
            + json.dumps(user_model.domain_skill_levels, sort_keys=True, default=str)
        )
    for observation in user_model.observations:
        parts.append(
            f"observation[{observation.domain}]: {observation.note} "
            f"(evidence: {observation.evidence})"
        )
    return "\n".join(parts)


def _coerce_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _knowledge_key(item: dict, index: int) -> str:
    return str(item.get("key") or f"item{index}")


def _knowledge_match_text(content: str, item: dict) -> str:
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple)) and tags:
        return content + " " + " ".join(str(t) for t in tags)
    return content


# --------------------------------------------------------------------------- #
# Candidate assembly + scoring
# --------------------------------------------------------------------------- #


def build_candidates(
    task: TaskSpec,
    *,
    memory: list[Memory] | None = None,
    user_model: UserModel | None = None,
    knowledge: list[dict] | None = None,
    task_state: WorkflowState | None = None,
    prior_outputs: dict[str, Any] | None = None,
) -> list[Candidate]:
    """Score every supplied input and return candidates in stable order.

    Candidates with zero relevance are still returned (so the compiler can
    report them as dropped); the compiler decides what actually survives.
    """
    query = query_tokens(task)
    candidates: list[Candidate] = []

    # task_state is the workflow anchor: always a candidate, and always at least
    # minimally relevant (Master section 15 flow diagram).
    if task_state is not None:
        content = task_state_content(task_state)
        base = max(overlap_count(query, content), 1)
        candidates.append(
            Candidate(
                key="task_state",
                content=content,
                source="task_state",
                score=base * WEIGHT_TASK_STATE,
                source_ref="task_state",
            )
        )

    # prior_outputs: outputs from preceding nodes in the execution pipeline.
    if prior_outputs and isinstance(prior_outputs, dict):
        clean_priors = filter_secrets(prior_outputs)
        for key in sorted(clean_priors.keys()):
            val = clean_priors[key]
            if val is None:
                continue
            content = _stringify(val)
            if not content.strip():
                continue
            overlap = overlap_count(query, content)
            base_score = max(overlap, 1)
            candidates.append(
                Candidate(
                    key=f"prior:{key}",
                    content=content,
                    source="prior_output",
                    score=base_score * WEIGHT_PRIOR_OUTPUT,
                    source_ref=f"prior_output:{key}",
                )
            )

    # memory: 0.8 * memory.relevance, plus a tiny recency tiebreak. The sort
    # order is deterministic (created_at, memory_id) so ties never depend on
    # list position.
    memories = [m for m in (memory or []) if isinstance(m, Memory)]
    ordered = sorted(memories, key=lambda m: (m.created_at, m.memory_id), reverse=True)
    count = len(ordered)
    for position, entry in enumerate(ordered):
        overlap = overlap_count(query, entry.content)
        score = overlap * WEIGHT_MEMORY * entry.relevance
        if overlap > 0:
            score += _RECENCY_EPS * (count - position)
        candidates.append(
            Candidate(
                key=f"memory:{entry.memory_id}",
                content=entry.content,
                source="memory",
                score=score,
                source_ref=f"memory:{entry.memory_id}",
            )
        )

    # user_model: 0.6 weight, only when there is something to say.
    if user_model is not None and (
        user_model.observations
        or user_model.preferences
        or user_model.domain_skill_levels
    ):
        content = user_model_content(user_model)
        candidates.append(
            Candidate(
                key="user_model",
                content=content,
                source="user_model",
                score=overlap_count(query, content) * WEIGHT_USER_MODEL,
                source_ref="user_model",
            )
        )

    # knowledge: (0.5 + provided score) weight.
    for index, item in enumerate(knowledge or []):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        weight = WEIGHT_KNOWLEDGE + _coerce_score(item.get("score"))
        source_ref = str(
            item.get("source_ref")
            or item.get("provenance")
            or f"knowledge:{_knowledge_key(item, index)}"
        )
        candidates.append(
            Candidate(
                key=f"knowledge:{_knowledge_key(item, index)}",
                content=content,
                source="knowledge",
                score=overlap_count(query, _knowledge_match_text(content, item))
                * weight,
                source_ref=source_ref,
            )
        )

    return candidates
