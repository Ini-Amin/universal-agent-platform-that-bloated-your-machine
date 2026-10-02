"""Versioned evaluator entities and runner (Master section 47).

Public API::

    from uap.evaluators import (
        EvaluatorSpec,
        EvaluationRecord,
        EvaluatorRegistry,
        EvaluatorRunner,
    )

    registry = EvaluatorRegistry()
    registry.add("has-sources", [
        {"name": "min-sources", "kind": "min_count",
         "params": {"field": "sources", "n": 2}},
    ])
    runner = EvaluatorRunner(registry)
    record = runner.run("has-sources@v1", {"sources": ["a", "b"]})
"""

from .model import EvaluationRecord, EvaluatorSpec
from .runner import EvaluatorRegistry, EvaluatorRunner

__all__ = [
    "EvaluationRecord",
    "EvaluatorRegistry",
    "EvaluatorRunner",
    "EvaluatorSpec",
]
