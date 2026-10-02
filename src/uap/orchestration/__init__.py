"""Orchestration adapters for the Universal Agent Platform (Master section 26).

Step 6 (LangGraph Integration) lives here. ``LangGraphWorkflowRunner`` is a
drop-in replacement for :class:`uap.workflows.runner.WorkflowRunner`, so the
platform can swap orchestrators without touching a single domain workflow
(Master section 29 rule 20: LangGraph is orchestration, not the architecture).

This package imports cleanly without ``langgraph`` installed: the dependency is
imported lazily by the runner's constructor, which raises a clear
``RuntimeError("LangGraph is required: use .venv-langgraph")`` when the package
is missing (the main Python 3.15 venv, where LangGraph has no wheels).
"""

from .langgraph_runner import LangGraphWorkflowRunner

__all__ = ["LangGraphWorkflowRunner"]
