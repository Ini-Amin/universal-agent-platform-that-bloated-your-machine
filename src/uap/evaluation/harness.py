"""Evaluation harness: run the section 23 metrics and report a single score.

The harness is the entry point a build step or CI job calls. It runs the
requested suites (all of them by default), aggregates their :class:`MetricResult`
objects into an :class:`EvalReport`, and can persist that report as a structured
artifact (Master section 23 + 19).

Nothing here calls a network or an LLM: every number is reproducible from the
checked-in datasets and the deterministic platform modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from uap.contracts.models import Artifact, ArtifactStatus, utc_now

from .datasets import DATASET_VERSION
from .metrics import (
    MetricResult,
    failure_rate,
    intent_accuracy,
    latency_stats,
    output_quality_score,
    routing_accuracy,
    tool_selection_accuracy,
    workflow_success_rate,
)

#: Suite name -> zero-argument callable returning a MetricResult.
SUITES: dict[str, object] = {
    "intent": intent_accuracy,
    "routing": routing_accuracy,
    "tool_selection": tool_selection_accuracy,
    "output_quality": output_quality_score,
    "workflow_success": lambda: workflow_success_rate(n=3),
    "failure": failure_rate,
    "latency": lambda: latency_stats(
        lambda: tool_selection_accuracy(), n=5, name="latency_stats"
    ),
}

#: The suite order used when the caller does not name any.
DEFAULT_SUITES: tuple[str, ...] = tuple(SUITES)

#: Artifact type emitted by :meth:`EvalReport.to_artifact`.
EVAL_ARTIFACT_TYPE = "eval-report.json"

#: Artifact source string for evaluation-produced artifacts.
EVAL_ARTIFACT_SOURCE = "evaluation"

# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@dataclass
class EvalReport:
    """Aggregated result of one harness run."""

    generated_at: datetime
    metrics: list[MetricResult] = field(default_factory=list)
    dataset_version: str = DATASET_VERSION
    overall_score: float = 0.0

    def __post_init__(self) -> None:
        self.overall_score = self._overall()

    def _overall(self) -> float:
        """Mean of the per-suite scores; 0.0 when there is nothing to average."""
        if not self.metrics:
            return 0.0
        return sum(metric.score for metric in self.metrics) / len(self.metrics)

    # -- serialisation ----------------------------------------------------- #

    def to_json(self) -> str:
        """Machine-readable report; parses back with ``json.loads``."""
        payload = {
            "generated_at": self.generated_at.isoformat(),
            "dataset_version": self.dataset_version,
            "overall_score": self.overall_score,
            "metrics": [
                {
                    "name": metric.name,
                    "passed": metric.passed,
                    "total": metric.total,
                    "score": metric.score,
                    "latency_ms": metric.latency_ms,
                    "details": metric.details,
                }
                for metric in self.metrics
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def summary(self) -> str:
        """Human-readable text table, one row per metric plus a total row."""
        lines = [
            f"Evaluation report ({self.dataset_version}) "
            f"generated {self.generated_at.isoformat()}",
            f"{'metric':<24}{'passed':>8}{'total':>8}{'score':>9}",
            "-" * 49,
        ]
        for metric in self.metrics:
            lines.append(
                f"{metric.name:<24}{metric.passed:>8}{metric.total:>8}"
                f"{metric.score:>9.3f}"
            )
        lines.append("-" * 49)
        lines.append(f"{'OVERALL':<24}{'':>8}{'':>8}{self.overall_score:>9.3f}")
        return "\n".join(lines)

    # -- artifact ---------------------------------------------------------- #

    def to_artifact(self, task_id: str) -> Artifact:
        """A structured artifact carrying this report (type ``eval-report.json``)."""
        return Artifact(
            task_id=task_id,
            type=EVAL_ARTIFACT_TYPE,
            source=EVAL_ARTIFACT_SOURCE,
            status=ArtifactStatus.FINAL,
            content_ref=self.to_json(),
        )

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class EvalHarness:
    """Runs evaluation suites and returns an :class:`EvalReport`."""

    def __init__(self, suites: list[str] | None = None) -> None:
        selected = list(suites) if suites is not None else list(DEFAULT_SUITES)
        unknown = [name for name in selected if name not in SUITES]
        if unknown:
            raise ValueError(
                f"unknown evaluation suite(s): {', '.join(sorted(unknown))}; "
                f"available: {', '.join(DEFAULT_SUITES)}"
            )
        self.suites: list[str] = selected

    def run(self) -> EvalReport:
        """Execute every selected suite and aggregate the results.

        Each suite reports under its own metric name (``intent_accuracy``,
        ``routing_accuracy``, ...), so the report is self-describing.
        """
        metrics: list[MetricResult] = [SUITES[name]() for name in self.suites]
        return EvalReport(generated_at=utc_now(), metrics=metrics)

__all__ = [
    "DEFAULT_SUITES",
    "EVAL_ARTIFACT_SOURCE",
    "EVAL_ARTIFACT_TYPE",
    "SUITES",
    "EvalHarness",
    "EvalReport",
]
