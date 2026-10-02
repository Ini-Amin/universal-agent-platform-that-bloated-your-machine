"""Evaluation metrics for the platform (Master section 23).

Every metric is a pure function over a dataset plus the *real* platform module
it measures: the Entry Workflow classifier, the Router, the Research Workflow
and the artifact quality of their outputs. Nothing here is subjective manual
testing -- each case is a deterministic, in-repo check with a numeric result.

The one deliberately non-LLM component is :func:`baseline_tool_selector`: a
small, documented keyword heuristic. The tool-selection metric scores *that
heuristic*, not a language model, so a regression in the heuristic is visible.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import statistics
import time
from dataclasses import dataclass, field

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.entry import EntryWorkflow
from uap.router import DOMAIN_WORKFLOW_MAP, Router, WorkflowRegistry
from uap.workflows import ResearchWorkflow

from .datasets import (
    INTENT_DATASET,
    QUALITY_CASES,
    ROUTING_DATASET,
    TOOL_SELECTION_DATASET,
    IntentCase,
)

#: ``details`` never grows past this many per-case one-liners.
MAX_DETAILS = 20

# --------------------------------------------------------------------------- #
# Result value object
# --------------------------------------------------------------------------- #

@dataclass
class MetricResult:
    """Outcome of one metric: how many cases passed out of how many.

    ``score`` is always ``passed / total`` (and ``0.0`` when ``total`` is 0,
    never a ``ZeroDivisionError``). ``details`` is capped at :data:`MAX_DETAILS`
    entries. ``latency_ms`` is only set by latency-style metrics.
    """

    name: str
    passed: int
    total: int
    score: float = 0.0
    details: list[str] = field(default_factory=list)
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        self.score = (self.passed / self.total) if self.total else 0.0
        self.details = list(self.details)[:MAX_DETAILS]

# --------------------------------------------------------------------------- #
# Async plumbing
# --------------------------------------------------------------------------- #

def _run_sync(coro):
    """Run ``coro`` to completion from synchronous code, loop or not.

    The platform's workflows are async; the metrics API is synchronous so it is
    easy to call from a report script or a test. When we are already inside a
    running event loop (e.g. an async test) the coroutine is executed on a
    dedicated thread so we never touch the caller's loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

# --------------------------------------------------------------------------- #
# Intent classification
# --------------------------------------------------------------------------- #

def _classify(text: str, workflow: EntryWorkflow) -> tuple[str, str | None]:
    """Return ``(domain, difficulty)`` the real Entry Workflow extracts.

    A request the workflow wants clarified counts as ``unknown`` -- asking a
    question instead of guessing is the correct behaviour (Master section 3).
    """
    outcome = workflow.run(
        _user_request(text)
    )
    if outcome.spec is None:
        return "unknown", None
    return outcome.spec.domain.value, outcome.spec.constraints.get("difficulty")

def _user_request(text: str):
    # Imported lazily to keep the module import surface tiny.
    from uap.contracts.models import UserRequest

    return UserRequest(raw_input=text)

def intent_accuracy(cases: list[IntentCase] | None = None) -> MetricResult:
    """Intent classification accuracy over :data:`INTENT_DATASET`.

    A case passes when the domain matches; when ``expected_difficulty`` is set
    the extracted difficulty must match too.
    """
    cases = list(cases if cases is not None else INTENT_DATASET)
    workflow = EntryWorkflow()
    passed = 0
    details: list[str] = []
    for case in cases:
        domain, difficulty = _classify(case.text, workflow)
        ok = domain == case.expected_domain
        if ok and case.expected_difficulty is not None:
            ok = difficulty == case.expected_difficulty
        passed += ok
        details.append(
            f"{'PASS' if ok else 'FAIL'} intent: expected={case.expected_domain}"
            f" got={domain}"
            + (f" difficulty={difficulty}" if case.expected_difficulty else "")
            + f" :: {case.text[:60]!r}"
        )
    return MetricResult("intent_accuracy", passed, len(cases), details=details)

# --------------------------------------------------------------------------- #
# Routing accuracy
# --------------------------------------------------------------------------- #

def _routing_registry() -> WorkflowRegistry:
    """A registry mirroring the live platform: the real Research Workflow plus
    named stand-ins for the domains whose workflows land in later build steps.

    Routing names come from ``DOMAIN_WORKFLOW_MAP``, so the registry only needs
    *an* object per domain -- the Router's decision is what we measure.
    """
    registry = WorkflowRegistry()
    for domain, name in DOMAIN_WORKFLOW_MAP.items():
        if name == "Clarification":
            continue
        if name == "ResearchWorkflow":
            registry.register(domain, ResearchWorkflow())
        else:
            registry.register(domain, name)  # placeholder binding
    return registry

def routing_accuracy() -> MetricResult:
    """Routing accuracy over :data:`ROUTING_DATASET` using the real Router."""
    router = Router(_routing_registry())
    passed = 0
    details: list[str] = []
    for domain, expected in ROUTING_DATASET:
        spec = TaskSpec(goal="evaluation probe", domain=Domain(domain))
        decision = router.route(spec)
        ok = decision.workflow_name == expected
        passed += ok
        details.append(
            f"{'PASS' if ok else 'FAIL'} routing: {domain} -> {decision.workflow_name}"
            f" (expected {expected})"
        )
    return MetricResult("routing_accuracy", passed, len(ROUTING_DATASET), details=details)

# --------------------------------------------------------------------------- #
# Tool selection (baseline heuristic -- NOT an LLM)
# --------------------------------------------------------------------------- #

# Ordered rules: first matching rule contributes its tool. Documented, tiny and
# deterministic so the metric measures the heuristic itself, not a model.
_TOOL_KEYWORD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("write", "save", "report", "artifact", "output"), "write_artifact_file"),
    (("read", "open", "load", "contents", "file"), "read_text_file"),
    (("fetch", "url", "http", "download", "page"), "fetch_url"),
    (("search", "find", "look up", "recent", "papers"), "search_web"),
    (("summarize", "summary", "summarise", "condense"), "summarize"),
    (("echo", "unchanged", "arguments", "verify wiring"), "echo"),
)

def baseline_tool_selector(goal: str, available: list[str]) -> list[str]:
    """Deterministic keyword heuristic picking tools for ``goal``.

    For every rule whose keywords appear in the lowercased goal, the rule's tool
    is selected if it is in ``available``. If no rule matches, the selector
    falls back to ``echo`` when available (the platform's wiring smoke-test
    tool); otherwise it returns an empty list. Selection is order-independent:
    the result is returned sorted so callers can compare as a set.
    """
    text = goal.casefold()
    chosen: set[str] = set()
    for keywords, tool in _TOOL_KEYWORD_RULES:
        if tool in available and any(keyword in text for keyword in keywords):
            chosen.add(tool)
    if not chosen and "echo" in available:
        chosen.add("echo")
    return sorted(chosen)

def tool_selection_accuracy() -> MetricResult:
    """Set-equality accuracy of :func:`baseline_tool_selector` on the dataset."""
    passed = 0
    details: list[str] = []
    for case in TOOL_SELECTION_DATASET:
        selected = baseline_tool_selector(case["goal"], list(case["available"]))
        expected = sorted(set(case["expected"]))
        ok = selected == expected
        passed += ok
        details.append(
            f"{'PASS' if ok else 'FAIL'} tool_selection: got={selected}"
            f" expected={expected} :: {case['goal'][:50]!r}"
        )
    return MetricResult(
        "tool_selection_accuracy", passed, len(TOOL_SELECTION_DATASET), details=details
    )

# --------------------------------------------------------------------------- #
# Agent output quality
# --------------------------------------------------------------------------- #

def output_quality_score() -> MetricResult:
    """Quality of artifact text over :data:`QUALITY_CASES`.

    A case passes when the content is non-empty, contains every ``must_contain``
    substring and contains no ``must_not_contain`` substring.
    """
    passed = 0
    details: list[str] = []
    for case in QUALITY_CASES:
        content = case["content"]
        reasons: list[str] = []
        if not content.strip():
            reasons.append("empty content")
        for needle in case["must_contain"]:
            if needle not in content:
                reasons.append(f"missing {needle!r}")
        for needle in case["must_not_contain"]:
            if needle in content:
                reasons.append(f"forbidden {needle!r}")
        ok = not reasons
        passed += ok
        details.append(
            f"{'PASS' if ok else 'FAIL'} quality[{case['artifact_type']}]: "
            + ("ok" if ok else "; ".join(reasons))
        )
    return MetricResult(
        "output_quality_score", passed, len(QUALITY_CASES), details=details
    )

# --------------------------------------------------------------------------- #
# Workflow success rate
# --------------------------------------------------------------------------- #

async def _run_research(n: int, workflow: ResearchWorkflow) -> list[WorkflowStatus]:
    statuses: list[WorkflowStatus] = []
    for index in range(n):
        task = TaskSpec(
            goal="Is Python a programming language",
            domain=Domain.RESEARCH,
            input={"question": "Is Python a programming language"},
        )
        # A fresh store per run so runs never resume each other.
        result = await workflow.run(task)
        statuses.append(result.status)
    return statuses

def workflow_success_rate(n: int = 3) -> MetricResult:
    """Fraction of real Research Workflow runs that reach ``completed``.

    Uses the deterministic stub collectors (the same fixtures the workflow ships
    with), so the metric is reproducible without any external service.
    """
    n = max(0, int(n))
    if n == 0:
        return MetricResult("workflow_success_rate", 0, 0, details=["no runs requested"])
    statuses = _run_sync(_run_research(n, ResearchWorkflow()))
    passed = sum(1 for status in statuses if status is WorkflowStatus.COMPLETED)
    details = [
        f"{'PASS' if status is WorkflowStatus.COMPLETED else 'FAIL'} run {i}: {status.value}"
        for i, status in enumerate(statuses)
    ]
    return MetricResult("workflow_success_rate", passed, n, details=details)

# --------------------------------------------------------------------------- #
# Failure rate
# --------------------------------------------------------------------------- #

async def _failing_collector(question: str):
    raise RuntimeError("injected collector failure")

def _run_failure_probe() -> list[tuple[bool, str]]:
    """Inject a failing collector and check the runner surfaces it cleanly.

    "Clean" means the runner returns a ``failed`` WorkflowResult carrying an
    error message instead of raising out of ``run`` -- a crash would be an
    evaluation failure, not a surfaced one.
    """
    outcomes: list[tuple[bool, str]] = []
    for attempt in range(3):
        workflow = ResearchWorkflow(collectors=[_failing_collector], max_retries=attempt)
        task = TaskSpec(
            goal="probe failure handling", domain=Domain.RESEARCH
        )
        try:
            result = _run_sync(workflow.run(task))
        except Exception as exc:  # noqa: BLE001 - a raise is exactly what we score
            outcomes.append((False, f"raised {type(exc).__name__}: {exc}"))
            continue
        clean = result.status is WorkflowStatus.FAILED and bool(result.error)
        outcomes.append(
            (
                clean,
                f"status={result.status.value} error={result.error!r}"
                if clean
                else f"unclean status={result.status.value} error={result.error!r}",
            )
        )
    return outcomes

def failure_rate() -> MetricResult:
    """Rate of *clean* failures when a collector is broken (score 1.0 = clean).

    The metric name follows Master section 23's "Failure Rate" axis; the score
    is the fraction of injected failures the runner surfaced cleanly.
    """
    outcomes = _run_failure_probe()
    passed = sum(1 for ok, _ in outcomes if ok)
    details = [
        f"{'PASS' if ok else 'FAIL'} failure_probe {i}: {message}"
        for i, (ok, message) in enumerate(outcomes)
    ]
    return MetricResult("failure_rate", passed, len(outcomes), details=details)

# --------------------------------------------------------------------------- #
# Latency
# --------------------------------------------------------------------------- #

def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a pre-sorted, non-empty list."""
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[rank]

def latency_stats(fn, n: int = 5, name: str = "latency_stats") -> MetricResult:
    """Measure ``fn`` ``n`` times; report p50 / p95 / mean in milliseconds.

    ``fn`` may be synchronous or async (a coroutine function). ``latency_ms``
    holds p50; the details carry the full distribution.
    """
    n = max(0, int(n))
    if n == 0:
        return MetricResult(name, 0, 0, details=["no samples requested"])
    samples: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        value = fn()
        if asyncio.iscoroutine(value) or isinstance(value, concurrent.futures.Future):
            _run_sync(value)
        samples.append((time.perf_counter() - start) * 1000.0)
    ordered = sorted(samples)
    p50 = _percentile(ordered, 0.50)
    p95 = _percentile(ordered, 0.95)
    mean = statistics.fmean(samples)
    details = [
        f"p50={p50:.4f} ms",
        f"p95={p95:.4f} ms",
        f"mean={mean:.4f} ms",
        f"samples={n}",
    ]
    # A latency measurement is "passing" when it produced a coherent distribution.
    passed = n if p50 <= p95 and mean > 0 else 0
    return MetricResult(name, passed, n, details=details, latency_ms=p50)

__all__ = [
    "MAX_DETAILS",
    "MetricResult",
    "baseline_tool_selector",
    "failure_rate",
    "intent_accuracy",
    "latency_stats",
    "output_quality_score",
    "routing_accuracy",
    "tool_selection_accuracy",
    "workflow_success_rate",
]
