"""Deterministic stages for the Entry Workflow (Master section 3).

Every stage here is pure and deterministic: no LLM, no tool calls, no agent
invocation, no domain execution. The stages only classify, check and compile.

A non-deterministic analyzer (LLM backed) can replace
``DeterministicIntentAnalyzer`` by implementing the same ``analyze`` method;
``EntryWorkflow`` accepts it through its ``intent_analyzer`` argument.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts.models import Domain, TaskMode, TaskSpec, UserRequest

__all__ = [
    "IntentResult",
    "DeterministicIntentAnalyzer",
    "intake",
    "check_sufficiency",
    "clarification_question",
    "check_constraints",
    "compile_task",
    "best_effort_goal",
]


# --------------------------------------------------------------------------- #
# Value object returned by every intent analyzer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntentResult:
    """What an intent analyzer understood from a request."""

    domain: Domain
    goal: str
    constraints: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    needs_clarification: bool = False
    targets: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Text normalisation helpers
# --------------------------------------------------------------------------- #

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, punctuation -> spaces, collapsed whitespace."""
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", text.lower())).strip()

# --------------------------------------------------------------------------- #
# Target and scope extraction (Master sections 3, 4 and 9)
# --------------------------------------------------------------------------- #
#
# The Entry Workflow stays lightweight: it only *recognises* targets and scope
# declarations so the Router and the BBP workflow have something structured to
# work with. It never resolves, scans or executes anything.

_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s<>\"'`]+")

# A dotted hostname whose last label is alphabetic. The lookbehind keeps the
# domain out of ``user@example.com`` and out of longer names
# (``api.example.com`` is matched whole, never as ``example.com``); the
# lookaheads stop a trailing sentence dot from extending the match.
_HOST_RE = re.compile(
    r"(?<![\w@.\-])"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}"
    r"(?![\w\-])(?!\.\w)"
)

_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

# Common source/data/document extensions that look like a TLD but are really
# filenames ("file.py", "notes.md"); such tokens are not targets.
_FILE_EXTENSIONS = frozenset(
    """
    py pyi pyc js mjs cjs ts tsx jsx txt md rst json jsonl yaml yml toml ini
    cfg conf html htm css scss less sass xml csv tsv log sh bash zsh fish bat
    cmd ps1 exe dll so dylib bin iso img tar gz bz2 xz zip rar 7z pdf doc docx
    xls xlsx ppt pptx png jpg jpeg gif bmp svg webp ico mp3 mp4 wav avi mov mkv
    c h cpp cc hpp java class go rs rb php sql db sqlite lock env bak tmp swp
    """.split()
)

def _valid_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in parts)

def _accept_host(value: str) -> str | None:
    """Normalise a candidate to a bare hostname, or ``None`` if it is not one."""
    host = value.strip().strip(".").lower()
    if not host:
        return None
    if _valid_ipv4(host):
        return host
    labels = host.split(".")
    if len(labels) < 2:
        return None
    tld = labels[-1]
    if not tld.isalpha() or len(tld) < 2 or tld in _FILE_EXTENSIONS:
        return None
    if any(not re.fullmatch(r"[a-z0-9\-]+", label) for label in labels):
        return None
    return host

def _host_from_url(url: str) -> str | None:
    rest = url.split("://", maxsplit=1)[1]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    authority = authority.rsplit("@", maxsplit=1)[-1]  # drop any userinfo
    authority = re.sub(r":\d+$", "", authority)  # drop the port
    return _accept_host(authority)

def _extract_targets(text: str) -> list[str]:
    """Deterministically pull hostnames/URLs out of raw text (order preserved).

    Emails are ignored (the ``@`` lookbehind stops the domain being lifted out
    of ``user@example.com``); version numbers and filenames are rejected by the
    alphabetic-TLD and file-extension checks.
    """
    found: list[tuple[int, str]] = []
    for match in _URL_RE.finditer(text):
        host = _host_from_url(match.group(0))
        if host:
            found.append((match.start(), host))
    for match in _HOST_RE.finditer(text):
        host = _accept_host(match.group(0))
        if host:
            found.append((match.start(), host))
    for match in _IPV4_RE.finditer(text):
        if _valid_ipv4(match.group(0)):
            found.append((match.start(), match.group(0)))

    targets: list[str] = []
    for _, host in sorted(found, key=lambda item: item[0]):
        if host not in targets:
            targets.append(host)
    return targets

_SCOPE_DECL_RE = re.compile(
    r"\b(?P<kind>out\s+of\s+scope|exclude|in\s+scope|scope)\s*:\s*"
    r"(?P<value>.+?)"
    r"(?=\s+\b(?:out\s+of\s+scope|exclude|in\s+scope|scope)\s*:|$)",
    re.IGNORECASE | re.DOTALL,
)

def _extract_scope(text: str) -> dict[str, list[str]]:
    """Parse explicit ``in scope:`` / ``out of scope:`` declarations (BBP only)."""
    result: dict[str, list[str]] = {"in_scope": [], "out_of_scope": []}
    for match in _SCOPE_DECL_RE.finditer(text):
        kind = match.group("kind").lower()
        key = "out_of_scope" if ("out" in kind or kind == "exclude") else "in_scope"
        for entry in re.split(r"[,\s]+", match.group("value").strip(" .\t\r\n")):
            entry = entry.strip().lower().strip(".")
            if entry and entry not in result[key]:
                result[key].append(entry)
    return {key: value for key, value in result.items() if value}

# --------------------------------------------------------------------------- #
# Keyword tables (Indonesian + English)
# --------------------------------------------------------------------------- #

# Ordered most-specific-phrase-first: the first phrase that matches wins, so
# "bug bounty" (bbp) is decided before a bare "bug" (coding) and
# "analisis data" (data) before a bare "analisis" (research).
_DOMAIN_KEYWORDS: tuple[tuple[str, Domain], ...] = (
    ("bug bounty", Domain.BBP),
    ("bug hunting", Domain.BBP),
    ("bug hunt", Domain.BBP),
    ("analisis data", Domain.DATA),
    ("analyze data", Domain.DATA),
    ("analyse data", Domain.DATA),
    ("data analysis", Domain.DATA),
    ("visualisasi data", Domain.DATA),
    ("visualize data", Domain.DATA),
    ("olah data", Domain.DATA),
    ("cari tahu", Domain.RESEARCH),
    ("find out", Domain.RESEARCH),
    # learning
    ("ajari", Domain.LEARNING),
    ("belajarkan", Domain.LEARNING),
    ("belajar", Domain.LEARNING),
    ("pelajari", Domain.LEARNING),
    ("mempelajari", Domain.LEARNING),
    ("subnetting", Domain.LEARNING),
    ("teach", Domain.LEARNING),
    ("explain", Domain.LEARNING),
    ("jelaskan", Domain.LEARNING),
    ("terangkan", Domain.LEARNING),
    ("tutorial", Domain.LEARNING),
    ("kursus", Domain.LEARNING),
    ("materi", Domain.LEARNING),
    ("how to", Domain.LEARNING),
    ("how do", Domain.LEARNING),
    ("how does", Domain.LEARNING),
    ("bagaimana cara", Domain.LEARNING),
    ("understand", Domain.LEARNING),
    ("memahami", Domain.LEARNING),
    ("pengertian", Domain.LEARNING),
    # research
    ("riset", Domain.RESEARCH),
    ("research", Domain.RESEARCH),
    ("investigate", Domain.RESEARCH),
    ("investigasi", Domain.RESEARCH),
    ("compare", Domain.RESEARCH),
    ("comparison", Domain.RESEARCH),
    ("bandingkan", Domain.RESEARCH),
    ("perbandingan", Domain.RESEARCH),
    ("teliti", Domain.RESEARCH),
    ("penelitian", Domain.RESEARCH),
    ("explore", Domain.RESEARCH),
    ("jelajahi", Domain.RESEARCH),
    ("analysis", Domain.RESEARCH),
    ("analisis", Domain.RESEARCH),
    ("study", Domain.RESEARCH),
    ("studi", Domain.RESEARCH),
    ("learn", Domain.LEARNING),
    # coding
    ("coding", Domain.CODING),
    ("ngoding", Domain.CODING),
    ("code", Domain.CODING),
    ("kode", Domain.CODING),
    ("implement", Domain.CODING),
    ("implementasi", Domain.CODING),
    ("implementasikan", Domain.CODING),
    ("refactor", Domain.CODING),
    ("refaktor", Domain.CODING),
    ("bug", Domain.CODING),
    ("kutu", Domain.CODING),
    ("program", Domain.CODING),
    ("develop", Domain.CODING),
    ("mengembangkan", Domain.CODING),
    ("pengembangan", Domain.CODING),
    ("fungsi", Domain.CODING),
    ("function", Domain.CODING),
    ("script", Domain.CODING),
    ("skrip", Domain.CODING),
    ("api", Domain.CODING),
    ("perbaiki", Domain.CODING),
    ("fix", Domain.CODING),
    ("deploy", Domain.CODING),
    ("compiler", Domain.CODING),
    ("library", Domain.CODING),
    # bbp
    ("recon", Domain.BBP),
    ("reconnaissance", Domain.BBP),
    ("vulnerability", Domain.BBP),
    ("kerentanan", Domain.BBP),
    ("celah keamanan", Domain.BBP),
    ("exploit", Domain.BBP),
    ("eksploitasi", Domain.BBP),
    ("payload", Domain.BBP),
    ("pentest", Domain.BBP),
    ("penetration testing", Domain.BBP),
    ("uji penetrasi", Domain.BBP),
    ("xss", Domain.BBP),
    ("sqli", Domain.BBP),
    ("rce", Domain.BBP),
    ("cve", Domain.BBP),
    ("hacking", Domain.BBP),
    ("hacker", Domain.BBP),
    ("keamanan", Domain.BBP),
    ("security", Domain.BBP),
    ("zeroday", Domain.BBP),
    ("zero day", Domain.BBP),
    # data
    ("dataset", Domain.DATA),
    ("data set", Domain.DATA),
    ("csv", Domain.DATA),
    ("spreadsheet", Domain.DATA),
    ("statistik", Domain.DATA),
    ("statistics", Domain.DATA),
    ("visualisasi", Domain.DATA),
    ("visualization", Domain.DATA),
    ("dashboard", Domain.DATA),
    ("data", Domain.DATA),
)

_BEGINNER_HINTS = (
    "beginner", "pemula", "dasar", "dari dasar", "dari nol", "dari awal",
    "from scratch", "from the basics", "from zero", "from the beginning",
    "basic", "sederhana", "mudah", "easy", "simple", "step by step",
    "langkah demi langkah", "newbie", "pengantar", "introduction", "not yet",
    "belum pernah", "belum paham", "baru pertama",
)

_ADVANCED_HINTS = (
    "advanced", "lanjutan", "expert", "mahir", "profesional", "professional",
    "kompleks", "complex", "mendalam", "in depth", "in-depth", "deep dive",
    "deep-dive", "tingkat tinggi", "sulit", "difficult", "hard", "rumit",
    "esoteric", "under the hood", "internal",
)

_AUTONOMOUS_HINTS = (
    "autonomous", "otomatis", "otomatisasi", "full auto", "hands off",
    "hands-off", "tanpa interaksi", "tanpa campur tangan", "unattended",
    "in the background", "secara mandiri", "jalankan sendiri", "berjalan sendiri",
    "sekaligus", "batch", "don t ask", "jangan tanya", "no questions",
    "fire and forget", "end to end",
)

_NO_VERIFY_HINTS = (
    "no verification", "skip verification", "without verification",
    "don t verify", "dont verify", "no need to verify", "tanpa verifikasi",
    "skip verifikasi", "tidak perlu verifikasi", "percaya saja",
)

# Function words used only to guess the request language, never the domain.
_ID_MARKERS = frozenset(
    """
    saya aku kamu anda sekalian kita kami ini itu dan atau serta yang untuk
    dari pada kepada di ke adalah tidak bukan bagaimana mengapa kenapa tolong
    mohon bantuan bantu jelaskan dengan bahwa karena maka saat ketika sudah
    belum akan bisa boleh perlu harap silakan menggunakan memakai dipakai dia
    mereka para sih kah lah ya juga masih saja sekali setiap beberapa banyak
    sedikit telah agar supaya jika kalau bila kemudian lalu setelah sebelum
    selama tetapi namun melainkan oleh sehingga yaitu merupakan sebagai
    secara tanpa interaksi otomatis otomatisasi manual langsung cepat lambat
    mau ingin berniat bermaksud melakukan dilakukan dikerjakan diperlukan
    penting kunci utama tambahan contoh contohnya misal misalnya seperti
    """.split()
)

_EN_MARKERS = frozenset(
    """
    the a an of to in on for with without please me you your my we our i is are
    was were be been do does did how what why when where who can could would
    should and or but about that this it its as at by from into over again more
    most very much also just only
    """.split()
)

_ID_QUESTIONS = {
    "domain": (
        "Jenis tugasnya belum jelas: belajar (learning), riset (research), "
        "coding, bug bounty (bbp), atau analisis data (data)?"
    ),
    "goal": (
        "Apa tujuan utama yang ingin dicapai? Jelaskan singkat, misalnya "
        "\"belajar subnetting dari dasar\"."
    ),
    "targets": (
        "Target mana yang ingin dinilai? Berikan domain atau URL "
        "(misalnya example.com)."
    ),
    "generic": "Apa yang sebenarnya ingin Anda kerjakan? Berikan detail lebih lanjut.",
}

_EN_QUESTIONS = {
    "domain": (
        "I cannot tell the task type yet: learning, research, coding, "
        "bug bounty (bbp), or data analysis (data)?"
    ),
    "goal": (
        "What is the main goal you want to achieve? Describe it briefly, e.g. "
        "\"teach me subnetting from scratch\"."
    ),
    "targets": (
        "Which target(s) should I assess? Provide a domain or URL "
        "(e.g. example.com)."
    ),
    "generic": "What exactly do you want to get done? Please add a little more detail.",
}

# Lead phrases that carry no goal content ("Ajari saya ...", "How do I ...").
_LEAD_NOISE = re.compile(
    r"^(?:please|kindly|tolong|mohon|bisakah|bisa|bantulah|bantu|ajari|belajarkan|"
    r"belajarlah|pelajari|mempelajari|teach|teach me|explain|explain to me|"
    r"jelaskan|terangkan|tunjukkan|show me|show|demonstrate|demo|how to|how do i|"
    r"how can i|how should i|how would i|how does|bagaimana cara|bagaimana|cara|"
    r"saya|aku|anda|kamu|kita|me|us|him|her|them|tentang|mengenai|perihal|about|"
    r"on the topic of|implement|implementasikan|mengimplementasikan|implementasi|"
    r"buat|membuat|membangun|mengembangkan|develop|write|create|build|make|fix|"
    r"perbaiki|sebuah|seorang|the|a|an)\b[\s,]*",
    re.IGNORECASE,
)

# Trailing qualifiers that are captured as constraints, not as the goal
# ("... dari dasar" / "... from scratch").
_TRAIL_NOISE = re.compile(
    r"[\s,]*(?:dari dasar|dari nol|dari awal|dari permulaan|from scratch|"
    r"from the basics|from zero|from the beginning|untuk pemula|buat pemula|"
    r"for beginners|step by step|langkah demi langkah|sederhana|yang sederhana|"
    r"yang mudah|dengan mudah|tolong ya|please)\s*$",
    re.IGNORECASE,
)

_GOAL_MAX_LEN = 240


def _extract_goal(text: str) -> str:
    """Reduce a request to a short goal phrase, deterministically."""
    goal = _normalize(text)
    if not goal:
        return ""
    previous = None
    while goal != previous:
        previous = goal
        goal = _LEAD_NOISE.sub("", goal, count=1).strip()
    goal = _TRAIL_NOISE.sub("", goal).strip()
    if not goal:  # do not strip everything away
        goal = _normalize(text)
    if len(goal) > _GOAL_MAX_LEN:
        goal = goal[: _GOAL_MAX_LEN - 1] + "…"
    return goal


def best_effort_goal(request: UserRequest) -> str:
    """Goal of last resort, used only when clarification rounds are exhausted."""
    return _normalize(request.raw_input)[:_GOAL_MAX_LEN]


def _detect_language(norm: str) -> str:
    words = frozenset(norm.split())
    id_hits = len(words & _ID_MARKERS)
    en_hits = len(words & _EN_MARKERS)
    return "id" if id_hits > en_hits else "en"


def _detect_difficulty(norm: str) -> str:
    if any(hint in norm for hint in _BEGINNER_HINTS):
        return "beginner"
    if any(hint in norm for hint in _ADVANCED_HINTS):
        return "advanced"
    return "intermediate"


def _detect_mode(norm: str) -> TaskMode:
    if any(hint in norm for hint in _AUTONOMOUS_HINTS):
        return TaskMode.AUTONOMOUS
    return TaskMode.INTERACTIVE


def _detect_verification(norm: str) -> bool:
    return not any(hint in norm for hint in _NO_VERIFY_HINTS)


# Domains checked first win exact ties; ambiguous words weigh only half.
_DOMAIN_PRIORITY = (
    Domain.LEARNING,
    Domain.RESEARCH,
    Domain.CODING,
    Domain.BBP,
    Domain.DATA,
)

_WEAK_SIGNALS = frozenset({"analisis", "analysis"})

# Unambiguous security / bug-bounty vocabulary. A request containing any of
# these is a BBP request even if coding or other domain words also appear
# (Master section 9); this keeps the Entry Workflow from misrouting, e.g.
# "Always prioritize authorization, scope compliance, ... Target: api.example.com".
_STRONG_BBP_SIGNALS = (
    "bug bounty",
    "bounty",
    "responsible disclosure",
    "in scope",
    "out of scope",
    "vulnerability",
    "authorization",
    "recon",
    "penetration",
    "pentest",
    "exploit",
    "cve",
)


def _detect_domain(norm: str) -> tuple[Domain, list[str]]:
    """Score every domain by its matched keywords; highest score wins.

    A phrase counts once per word it contains, so specific multi-word phrases
    ("bug bounty", "analisis data") outweigh single tokens. Genuine straddlers
    ("analisis"/"analysis") count only half to let a concrete topic token
    ("dataset") pick its own domain. Ties break by ``_DOMAIN_PRIORITY``.
    """
    scores: dict[Domain, float] = {}
    matches: list[str] = []
    for phrase, candidate in _DOMAIN_KEYWORDS:
        if not _has_phrase(norm, phrase):
            continue
        matches.append(phrase)
        scores[candidate] = scores.get(candidate, 0.0) + _phrase_weight(phrase)
    # A strong security/bounty signal outranks every other domain (section 9),
    # even when the signal is not itself a keyword-table entry ("authorization").
    if any(_has_phrase(norm, signal) for signal in _STRONG_BBP_SIGNALS):
        return Domain.BBP, matches
    if not scores:
        return Domain.UNKNOWN, []
    best = max(
        _DOMAIN_PRIORITY,
        key=lambda domain: (scores.get(domain, 0.0), -_DOMAIN_PRIORITY.index(domain)),
    )
    return best, matches


def _has_phrase(norm: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", norm) is not None


def _phrase_weight(phrase: str) -> float:
    if phrase in _WEAK_SIGNALS:
        return 0.5
    return float(len(phrase.split()))


# --------------------------------------------------------------------------- #
# Stage 1: Intake
# --------------------------------------------------------------------------- #


def intake(request: UserRequest, clarification: str | None = None) -> UserRequest:
    """Validate and merge a clarification answer into the working request.

    No analysis happens here: the stage only produces the text the rest of the
    pipeline will look at.
    """
    text = request.raw_input.strip()
    if clarification and clarification.strip():
        answer = clarification.strip()
        text = f"{text}\n{answer}" if text else answer
    return UserRequest(
        raw_input=text,
        user_id=request.user_id,
        session_id=request.session_id,
        created_at=request.created_at,
        metadata=request.metadata,
    )


# --------------------------------------------------------------------------- #
# Stage 2: Intent Analysis (default, deterministic implementation)
# --------------------------------------------------------------------------- #


class DeterministicIntentAnalyzer:
    """Rule/keyword based classifier for Indonesian and English requests.

    Stateless and deterministic: identical input always yields identical output.
    """

    def analyze(self, request: UserRequest) -> IntentResult:
        norm = _normalize(request.raw_input)
        domain, matches = _detect_domain(norm)
        goal = _extract_goal(request.raw_input)
        targets = _extract_targets(request.raw_input)
        constraints = {
            "language": _detect_language(norm),
            "difficulty": _detect_difficulty(norm),
            "mode": _detect_mode(norm).value,
            "verification": _detect_verification(norm),
        }
        # Scope declarations are meaningful only for BBP (Master section 9).
        if domain == Domain.BBP:
            constraints.update(_extract_scope(request.raw_input))
        confidence = 0.0
        if matches:
            confidence = min(0.95, 0.55 + 0.1 * (len(matches) - 1))
        return IntentResult(
            domain=domain,
            goal=goal,
            constraints=constraints,
            confidence=confidence,
            needs_clarification=not goal.strip(),
            targets=targets,
        )


# --------------------------------------------------------------------------- #
# Stage 3: Context Sufficiency
# --------------------------------------------------------------------------- #

MIN_GOAL_LEN = 3


def check_sufficiency(intent: IntentResult) -> tuple[bool, str]:
    """Decide whether the intent is clear enough to compile a TaskSpec.

    Returns ``(sufficient, reason)``; ``reason`` is empty when sufficient.
    """
    if not intent.goal.strip() or len(intent.goal.strip()) < MIN_GOAL_LEN:
        return False, "goal"
    if intent.needs_clarification:
        return False, "intent"
    if intent.domain == Domain.UNKNOWN:
        return False, "domain"
    if (
        intent.domain == Domain.BBP
        and not intent.targets
        and not intent.constraints.get("in_scope")
    ):
        # BBP needs *something* to assess. Scope policy is enforced later by
        # the server/workflow, so only the target is required at entry.
        return False, "targets"
    return True, ""


def clarification_question(intent: IntentResult, language: str) -> str:
    """Pick the single most useful question to ask the user."""
    table = _ID_QUESTIONS if language == "id" else _EN_QUESTIONS
    _, reason = check_sufficiency(intent)
    return table.get(reason, table["generic"])


# --------------------------------------------------------------------------- #
# Stage 4: Constraint Check
# --------------------------------------------------------------------------- #


def check_constraints(
    intent: IntentResult, request: UserRequest
) -> tuple[dict[str, Any], TaskMode, bool]:
    """Merge detected constraints with user supplied ones (user wins)."""
    detected = dict(intent.constraints)
    user_supplied = request.metadata.get("constraints")
    if not isinstance(user_supplied, dict):
        user_supplied = {}

    mode_hint = user_supplied.get("mode", detected.get("mode"))
    try:
        mode = TaskMode(mode_hint)
    except ValueError:
        mode = TaskMode.INTERACTIVE

    verification_hint = user_supplied.get("verification", detected.get("verification"))
    verification = bool(verification_hint)

    constraints = {**detected, **user_supplied}
    constraints.pop("mode", None)
    constraints.pop("verification", None)
    return constraints, mode, verification


# --------------------------------------------------------------------------- #
# Stage 5: Task Compiler
# --------------------------------------------------------------------------- #


def compile_task(
    request: UserRequest,
    intent: IntentResult,
    constraints: dict[str, Any],
    mode: TaskMode,
    verification: bool,
) -> TaskSpec:
    task_input: dict[str, Any] = {"raw": request.raw_input}
    if intent.targets:
        task_input["targets"] = list(intent.targets)
    return TaskSpec(
        domain=intent.domain,
        goal=intent.goal,
        input=task_input,
        constraints=constraints,
        mode=mode,
        verification=verification,
    )


def session_key(request: UserRequest) -> str:
    """Stable key under which clarification rounds are counted.

    Prefers explicit session/user ids; falls back to the request text so a
    caller that reuses the same ``UserRequest`` object keeps its round counter.
    """
    for candidate in (request.session_id, request.user_id):
        if candidate:
            return candidate
    digest = hashlib.sha1(request.raw_input.encode("utf-8")).hexdigest()
    return f"anon:{digest[:16]}"
