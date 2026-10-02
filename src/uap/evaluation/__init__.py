"""Evaluation (Master section 23) - build Step 16.

Usage::

    from uap.evaluation import EvalHarness, EvalReport

    report = EvalHarness().run()          # all suites
    print(report.summary())
    artifact = report.to_artifact(task_id)

The suites measure the real platform modules: the Entry Workflow classifier,
the Router, the Research Workflow and artifact output quality -- never
subjective manual testing.
"""

from .datasets import (
    DATASET_VERSION,
    INTENT_DATASET,
    QUALITY_CASES,
    ROUTING_DATASET,
    TOOL_SELECTION_DATASET,
    IntentCase,
)
from .harness import (
    DEFAULT_SUITES,
    EVAL_ARTIFACT_SOURCE,
    EVAL_ARTIFACT_TYPE,
    SUITES,
    EvalHarness,
    EvalReport,
)
from .metrics import (
    MAX_DETAILS,
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

__all__ = [
    "DATASET_VERSION",
    "DEFAULT_SUITES",
    "EVAL_ARTIFACT_SOURCE",
    "EVAL_ARTIFACT_TYPE",
    "INTENT_DATASET",
    "MAX_DETAILS",
    "QUALITY_CASES",
    "ROUTING_DATASET",
    "SUITES",
    "TOOL_SELECTION_DATASET",
    "EvalHarness",
    "EvalReport",
    "IntentCase",
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
