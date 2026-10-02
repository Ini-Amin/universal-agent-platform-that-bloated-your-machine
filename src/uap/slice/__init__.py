"""§71 vertical slice: end-to-end platform spine for one user request.

Exposes the orchestrator (:class:`PlatformSlice` + :class:`SliceResult`), the
graph node runtime (:class:`PlatformNodeRuntime`) and the slice evaluation gate
(:class:`SliceEvaluator`).
"""

from __future__ import annotations

from .evaluation_gate import SliceEvaluator
from .orchestrator import PlatformSlice, SliceResult, build_research_graph
from .runtime_adapter import PlatformNodeRuntime

__all__ = [
    "PlatformSlice",
    "SliceResult",
    "PlatformNodeRuntime",
    "SliceEvaluator",
    "build_research_graph",
]
