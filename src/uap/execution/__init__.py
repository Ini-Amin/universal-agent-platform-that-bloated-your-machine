"""Graph execution engine (Master sections 18-21, 35-37).

The public surface of this package:

* :class:`GraphExecutor` - deterministic scheduler for a canonical
  :class:`~uap.graph.model.WorkflowGraph`.
* :class:`ExecutionContext` / :class:`ExecutionResult` - run state and outcome.
* :class:`NodeRuntime` - the protocol a caller injects to actually run a node.
* :class:`NodeExecutionError` / :class:`PauseExecution` - node failure and pause
  control flow.
* :func:`evaluate` - the safe, deterministic condition evaluator.
"""

from .conditions import evaluate
from .context import (
    ExecutionContext,
    ExecutionResult,
    NodeExecutionError,
    NodeRuntime,
    PauseExecution,
)
from .engine import GraphExecutor

__all__ = [
    "GraphExecutor",
    "ExecutionContext",
    "ExecutionResult",
    "NodeRuntime",
    "NodeExecutionError",
    "PauseExecution",
    "evaluate",
]
