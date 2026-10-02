"""Tests for the evaluation harness (build Step 16, Master section 23).

These tests double as the build's evidence that the platform works: the intent,
routing, tool-selection, quality, workflow-success and failure metrics all run
the *real* platform modules (Entry Workflow, Router, Research Workflow).
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from uap.contracts.models import Artifact
from uap.evaluation import (
    DEFAULT_SUITES,
    INTENT_DATASET,
    MAX_DETAILS,
    QUALITY_CASES,
    ROUTING_DATASET,
    TOOL_SELECTION_DATASET,
    EvalHarness,
    EvalReport,
    IntentCase,
    MetricResult,
    baseline_tool_selector,
    failure_rate,
    intent_accuracy,
    latency_stats,
    output_quality_score,
    routing_accuracy,
    tool_selection_accuracy,
    workflow_success_rate,
)

# --------------------------------------------------------------------------- #
# 1. Dataset shape: size and balance
# --------------------------------------------------------------------------- #

def test_intent_dataset_is_large_and_balanced():
    assert len(INTENT_DATASET) >= 20

    counts = Counter(case.expected_domain for case in INTENT_DATASET)
    # Every concrete domain plus unknown is represented, and each of the five
    # real domains carries the same number of cases (balanced).
    for domain in ("learning", "research", "coding", "bbp", "data"):
        assert counts[domain] >= 1
    real = [counts[d] for d in ("learning", "research", "coding", "bbp", "data")]
    assert len(set(real)) == 1, f"unbalanced domains: {counts}"
    assert counts["unknown"] >= 2
    # Bilingual: at least some Indonesian and some English requests.
    assert any(case.text.startswith(("Ajari", "Jelaskan", "Riset", "Bandingkan"))
               for case in INTENT_DATASET)
    assert any("Research" in case.text or "Explain" in case.text
               for case in INTENT_DATASET)

def test_routing_and_tool_datasets_cover_their_contracts():
    assert {domain for domain, _ in ROUTING_DATASET} == {
        "learning", "research", "coding", "bbp", "data", "unknown",
    }
    assert len(TOOL_SELECTION_DATASET) >= 8
    assert len(QUALITY_CASES) >= 6
    for case in QUALITY_CASES:
        assert case["artifact_type"] in {"report.md", "sources.json"}

# --------------------------------------------------------------------------- #
# 2. Intent accuracy against the real Entry Workflow
# --------------------------------------------------------------------------- #

def test_intent_accuracy_on_real_entry_workflow_is_high():
    metric = intent_accuracy()
    # Report the actual number in the failure message for evidence.
    assert metric.score >= 0.8, f"intent accuracy {metric.passed}/{metric.total}"
    assert metric.total == len(INTENT_DATASET)

# --------------------------------------------------------------------------- #
# 3. Intent metric arithmetic on a hand-built case list
# --------------------------------------------------------------------------- #

def test_intent_accuracy_counts_one_wrong_case():
    cases = [
        IntentCase("Research the trade-offs of SQL databases", "research"),  # right
        IntentCase("Ajari saya subnetting dari dasar", "research"),  # wrong (learning)
    ]
    metric = intent_accuracy(cases)
    assert metric.total == 2
    assert metric.passed == 1
    assert metric.score == 0.5

# --------------------------------------------------------------------------- #
# 4. Routing accuracy with the real Router
# --------------------------------------------------------------------------- #

def test_routing_accuracy_is_perfect_with_real_router():
    metric = routing_accuracy()
    assert metric.total == 6
    assert metric.passed == 6
    assert metric.score == 1.0

# --------------------------------------------------------------------------- #
# 5. Tool selection with the baseline heuristic
# --------------------------------------------------------------------------- #

def test_tool_selection_accuracy_with_baseline_selector():
    metric = tool_selection_accuracy()
    assert metric.total == len(TOOL_SELECTION_DATASET)
    assert metric.score >= 0.75, f"tool selection {metric.passed}/{metric.total}"

def test_baseline_selector_is_set_and_order_independent():
    # available order must not change the result
    a = baseline_tool_selector("Read the file then write a report", ["echo", "write_artifact_file", "read_text_file"])
    b = baseline_tool_selector("Read the file then write a report", ["read_text_file", "write_artifact_file", "echo"])
    assert a == b == ["read_text_file", "write_artifact_file"]
    # no keyword match -> documented echo fallback
    assert baseline_tool_selector("Do the needful", ["echo"]) == ["echo"]

# --------------------------------------------------------------------------- #
# 6. Output quality: good passes, empty fails, missing sources fails
# --------------------------------------------------------------------------- #

def test_output_quality_good_empty_and_missing_sources():
    metric = output_quality_score()
    assert metric.total == len(QUALITY_CASES)

    details = metric.details
    # QUALITY_CASES[0] is the good report; [4] misses provenance; [5] is empty.
    assert details[0].startswith("PASS"), details[0]
    assert details[4].startswith("FAIL") and "missing" in details[4], details[4]
    assert details[5].startswith("FAIL") and "empty" in details[5], details[5]

# --------------------------------------------------------------------------- #
# 7. Workflow success rate against the real Research Workflow
# --------------------------------------------------------------------------- #

def test_workflow_success_rate_runs_real_research_workflow():
    metric = workflow_success_rate(n=2)
    assert metric.total == 2
    assert metric.passed == 2
    assert metric.score == 1.0

# --------------------------------------------------------------------------- #
# 8. Failure rate: broken collector surfaced cleanly
# --------------------------------------------------------------------------- #

def test_failure_rate_surfaces_clean_failures():
    metric = failure_rate()
    # score 1.0 == every injected failure became a clean ``failed`` result,
    # never an exception escaping the runner.
    assert metric.total >= 1
    assert metric.score == 1.0, metric.details
    assert all("raised" not in line for line in metric.details)

# --------------------------------------------------------------------------- #
# 9. Latency statistics
# --------------------------------------------------------------------------- #

def _parse_latency(details: list[str]) -> tuple[float, float, float]:
    def value(prefix: str) -> float:
        for line in details:
            if line.startswith(prefix):
                return float(line.split("=", 1)[1].replace("ms", "").strip())
        raise AssertionError(f"missing {prefix} in {details}")

    return value("p50"), value("p95"), value("mean")

def test_latency_stats_ordering_and_mean():
    metric = latency_stats(lambda: sum(range(1000)), n=5)
    assert metric.latency_ms is not None
    p50, p95, mean = _parse_latency(metric.details)
    assert p50 <= p95
    assert mean > 0
    # latency_ms is the p50 (details round to 4dp, hence the tolerance).
    assert metric.latency_ms == pytest.approx(p50, abs=1e-3)

# --------------------------------------------------------------------------- #
# 10. MetricResult score math
# --------------------------------------------------------------------------- #

def test_metric_result_score_math_and_zero_total():
    assert MetricResult("m", 3, 4).score == 0.75
    assert MetricResult("m", 0, 0).score == 0.0  # no ZeroDivisionError
    assert MetricResult("m", 5, 5).score == 1.0

# --------------------------------------------------------------------------- #
# 11. EvalReport JSON
# --------------------------------------------------------------------------- #

def test_eval_report_to_json_parses_and_covers_all_metrics():
    report = EvalHarness().run()
    payload = json.loads(report.to_json())

    assert 0.0 <= payload["overall_score"] <= 1.0
    assert payload["overall_score"] == report.overall_score
    names = {metric["name"] for metric in payload["metrics"]}
    # Every metric the report exposes is serialised, one row per suite.
    assert names == {metric.name for metric in report.metrics}
    assert {"intent_accuracy", "routing_accuracy", "tool_selection_accuracy",
            "output_quality_score", "workflow_success_rate", "failure_rate"} <= names
    for metric in payload["metrics"]:
        assert 0.0 <= metric["score"] <= 1.0
        assert metric["passed"] <= metric["total"]

# --------------------------------------------------------------------------- #
# 12. to_artifact
# --------------------------------------------------------------------------- #

def test_to_artifact_produces_valid_eval_report_artifact():
    report = EvalHarness().run()
    artifact = report.to_artifact("task-eval-1")

    assert isinstance(artifact, Artifact)
    assert artifact.type == "eval-report.json"
    assert artifact.source == "evaluation"
    assert artifact.task_id == "task-eval-1"
    # The artifact content is the report JSON and round-trips.
    assert json.loads(artifact.content_ref)["overall_score"] == report.overall_score

# --------------------------------------------------------------------------- #
# 13. Full harness run + summary
# --------------------------------------------------------------------------- #

def test_full_harness_run_covers_default_suites_and_summarises():
    harness = EvalHarness()
    report = harness.run()

    assert len(report.metrics) == len(DEFAULT_SUITES)
    assert {metric.name for metric in report.metrics} >= {
        "intent_accuracy", "routing_accuracy", "tool_selection_accuracy",
        "output_quality_score", "workflow_success_rate", "failure_rate",
        "latency_stats",
    }
    summary = report.summary()
    assert summary.strip()
    assert "OVERALL" in summary

def test_harness_selects_a_subset_and_rejects_unknown_suites():
    report = EvalHarness(["routing"]).run()
    assert [metric.name for metric in report.metrics] == ["routing_accuracy"]

    with pytest.raises(ValueError, match="nope"):
        EvalHarness(["nope"])

# --------------------------------------------------------------------------- #
# 14. details are capped
# --------------------------------------------------------------------------- #

def test_details_are_capped_at_twenty():
    cases = [IntentCase(f"Research topic number {i}", "research") for i in range(30)]
    metric = intent_accuracy(cases)
    assert metric.total == 30
    assert len(metric.details) == MAX_DETAILS == 20
    # A directly constructed MetricResult is capped too.
    assert len(MetricResult("m", 0, 1, details=[str(i) for i in range(50)]).details) == 20
