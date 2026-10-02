"""Versioned, in-repo evaluation datasets (Master section 23).

Every dataset here is a plain Python literal so evaluation needs no network,
no LLM and no fixture files: the numbers a build reports can always be
reproduced from the same commit (Master section 29.11: prefer deterministic
logic wherever deterministic logic is sufficient).

Four datasets cover the section 23 axes:

* :data:`INTENT_DATASET`        - intent classification (Entry Workflow).
* :data:`ROUTING_DATASET`       - routing accuracy (Router).
* :data:`TOOL_SELECTION_DATASET`- tool selection (keyword baseline heuristic).
* :data:`QUALITY_CASES`         - agent output quality (artifact text checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Bump when any case below changes, so a score can be tied to a dataset rev.
DATASET_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# Intent classification
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IntentCase:
    """One labelled request for the Entry Workflow classifier.

    ``expected_difficulty`` is optional: when set, the metric also checks the
    difficulty constraint the analyzer extracted, not only the domain.
    """

    text: str
    expected_domain: str
    expected_difficulty: str | None = None

# Bilingual (Indonesian + English), balanced: 5 cases for each of the five
# concrete domains plus 2 genuinely domain-less requests.
INTENT_DATASET: list[IntentCase] = [
    # learning (5)
    IntentCase("Ajari saya subnetting dari dasar", "learning", "beginner"),
    IntentCase("Explain how DNS resolution works", "learning"),
    IntentCase("Jelaskan konsep dasar pemrograman fungsional", "learning", "beginner"),
    IntentCase(
        "Buatkan tutorial langkah demi langkah untuk memahami Docker",
        "learning",
        "beginner",
    ),
    IntentCase(
        "Jelaskan secara mendalam cara kerja TLS handshake", "learning", "advanced"
    ),
    # research (5)
    IntentCase(
        "Research the performance trade-offs between Python web frameworks",
        "research",
    ),
    IntentCase("Bandingkan PostgreSQL dan MySQL untuk beban kerja analitik", "research"),
    IntentCase("Investigate recent advances in quantum error correction", "research"),
    IntentCase("Riset terbaru tentang arsitektur transformer", "research"),
    IntentCase("Compare the trade-offs of SQL and NoSQL databases", "research"),
    # coding (5)
    IntentCase("Refactor the authentication module and fix the bug", "coding"),
    IntentCase("Write a Python script to parse log files", "coding"),
    IntentCase("Implement a binary search function in Go", "coding"),
    IntentCase("Tolong perbaiki kode Python saya yang error", "coding"),
    IntentCase("Deploy the service with a CI pipeline and refactor the library", "coding"),
    # bbp (5)
    IntentCase(
        "Bug bounty recon and vulnerability scanning on example.com", "bbp"
    ),
    IntentCase("Find XSS and SQLi vulnerabilities in the login form", "bbp"),
    IntentCase("Pentest the API for security issues", "bbp"),
    IntentCase("Eksploitasi celah keamanan pada server target", "bbp"),
    IntentCase("Analyze the CVE and write an exploit payload", "bbp"),
    # data (5)
    IntentCase("Analyze the sales dataset and build a dashboard", "data"),
    IntentCase("Buat visualisasi data penjualan bulanan", "data"),
    IntentCase("Olah data CSV menjadi statistik ringkas", "data"),
    IntentCase("Compute descriptive statistics for the experiment results", "data"),
    IntentCase("Clean the dataset and produce a summary table", "data"),
    # unknown (2) - the Entry Workflow must ask for clarification, not guess
    IntentCase("Halo, apa kabar hari ini?", "unknown"),
    IntentCase("Thanks, that's all for now.", "unknown"),
]

# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

# (domain, expected workflow name). Mirrors DOMAIN_WORKFLOW_MAP (Master section
# 5) and covers all six domains, including the Clarification fallback.
ROUTING_DATASET: list[tuple[str, str]] = [
    ("learning", "LearningWorkflow"),
    ("research", "ResearchWorkflow"),
    ("coding", "CodingWorkflow"),
    ("bbp", "BBPWorkflow"),
    ("data", "DataWorkflow"),
    ("unknown", "Clarification"),
]

# --------------------------------------------------------------------------- #
# Tool selection
# --------------------------------------------------------------------------- #

# Each case is {goal, available, expected}; ``expected`` is compared as a set.
# The local tools (echo / read_text_file / write_artifact_file) come from
# uap.tools.local; the mock names (fetch_url, search_web, summarize) stand in
# for capabilities that arrive in later build steps (Master section 12).
TOOL_SELECTION_DATASET: list[dict[str, Any]] = [
    {
        "goal": "Write the final report to a file",
        "available": ["write_artifact_file", "echo"],
        "expected": ["write_artifact_file"],
    },
    {
        "goal": "Read the contents of notes.txt",
        "available": ["read_text_file", "echo"],
        "expected": ["read_text_file"],
    },
    {
        "goal": "Echo back the arguments to verify wiring",
        "available": ["echo", "read_text_file"],
        "expected": ["echo"],
    },
    {
        "goal": "Fetch the page at the given URL",
        "available": ["fetch_url", "write_artifact_file"],
        "expected": ["fetch_url"],
    },
    {
        "goal": "Search the web for recent papers",
        "available": ["search_web", "fetch_url"],
        "expected": ["search_web"],
    },
    {
        "goal": "Read the file then write a summary report",
        "available": ["read_text_file", "write_artifact_file", "echo"],
        "expected": ["read_text_file", "write_artifact_file"],
    },
    {
        "goal": "Summarize the collected evidence",
        "available": ["summarize", "write_artifact_file"],
        "expected": ["summarize"],
    },
    {
        "goal": "Echo the input and write it to an artifact",
        "available": ["echo", "write_artifact_file"],
        "expected": ["echo", "write_artifact_file"],
    },
    {
        "goal": "Return the tool arguments unchanged",
        "available": ["echo"],
        "expected": ["echo"],
    },
    {
        "goal": "Open the configuration file",
        "available": ["read_text_file"],
        "expected": ["read_text_file"],
    },
    {
        "goal": "Do the needful",
        "available": ["echo"],
        "expected": ["echo"],  # no keyword matches -> documented echo fallback
    },
]

# --------------------------------------------------------------------------- #
# Agent output quality
# --------------------------------------------------------------------------- #

# Each case is {artifact_type, content, must_contain, must_not_contain}. The
# metric fails a case when the content is empty, when any ``must_contain``
# substring is missing, or when any ``must_not_contain`` substring is present.
# The set deliberately mixes valid outputs (cases 1-4) with malformed ones
# (cases 5-8: missing provenance, empty, missing URL, leftover placeholder) so
# the checker is proven to reject bad artifacts, not just accept good ones.
QUALITY_CASES: list[dict[str, Any]] = [
    {
        # 1. A good report: has a title, verified findings, provenance and URLs.
        "artifact_type": "report.md",
        "content": (
            "# Research Report\n"
            "\n"
            "## Question\n"
            "\n"
            "Is Python a programming language?\n"
            "\n"
            "## Verified findings\n"
            "\n"
            "- Python is a programming language\n"
            "  - sources: docs, papers, web\n"
            "  - https://docs.python.org/3/\n"
            "\n"
            "## Evidence base\n"
            "\n"
            "3 evidence items across 3 sources (docs, papers, web).\n"
        ),
        "must_contain": ["# Research Report", "Verified findings", "sources:", "http"],
        "must_not_contain": ["TODO", "lorem ipsum"],
    },
    {
        # 2. A good sources.json: each record carries source, claim and url.
        "artifact_type": "sources.json",
        "content": (
            "[\n"
            "  {\n"
            '    "source": "web",\n'
            '    "claim": "Python is a programming language",\n'
            '    "url": "https://example.com/python",\n'
            '    "confidence": 0.92\n'
            "  }\n"
            "]\n"
        ),
        "must_contain": ['"source"', '"claim"', '"url"'],
        "must_not_contain": ["TODO"],
    },
    {
        # 3. A good short report with explicit source lines.
        "artifact_type": "report.md",
        "content": (
            "# Research Report\n"
            "\n"
            "## Verified findings\n"
            "\n"
            "- The GIL limits true parallelism\n"
            "  - sources: papers, web\n"
            "  - https://example.com/gil\n"
        ),
        "must_contain": ["# Research Report", "sources:", "http"],
        "must_not_contain": ["TODO"],
    },
    {
        # 4. A good sources.json with two well-formed records.
        "artifact_type": "sources.json",
        "content": (
            "[\n"
            '  {"source": "web", "claim": "A", "url": "https://example.com/a"},\n'
            '  {"source": "docs", "claim": "B", "url": "https://example.com/b"}\n'
            "]\n"
        ),
        "must_contain": ['"source"', '"claim"', '"url"'],
        "must_not_contain": ["TODO"],
    },
    {
        # 5. Missing provenance: no per-claim "sources:" line and no URLs.
        "artifact_type": "report.md",
        "content": (
            "# Research Report\n"
            "\n"
            "## Question\n"
            "\n"
            "Is Python fast?\n"
            "\n"
            "## Verified findings\n"
            "\n"
            "- none\n"
            "\n"
            "## Evidence base\n"
            "\n"
            "0 evidence items across 0 sources (none).\n"
        ),
        "must_contain": ["# Research Report", "sources:", "http"],
        "must_not_contain": ["TODO"],
    },
    {
        # 6. Empty output must always fail.
        "artifact_type": "report.md",
        "content": "",
        "must_contain": ["# Research Report"],
        "must_not_contain": [],
    },
    {
        # 7. Source record without a URL is not evidence.
        "artifact_type": "sources.json",
        "content": (
            "[\n"
            "  {\n"
            '    "source": "web",\n'
            '    "claim": "Python is a programming language",\n'
            '    "confidence": 0.92\n'
            "  }\n"
            "]\n"
        ),
        "must_contain": ['"url"'],
        "must_not_contain": [],
    },
    {
        # 8. A placeholder left in the output must be rejected.
        "artifact_type": "report.md",
        "content": (
            "# Research Report\n"
            "\n"
            "## Question\n"
            "\n"
            "TODO: fill this in\n"
            "\n"
            "## Verified findings\n"
            "\n"
            "- none\n"
        ),
        "must_contain": ["# Research Report"],
        "must_not_contain": ["TODO"],
    },
]

__all__ = [
    "DATASET_VERSION",
    "IntentCase",
    "INTENT_DATASET",
    "ROUTING_DATASET",
    "TOOL_SELECTION_DATASET",
    "QUALITY_CASES",
]
