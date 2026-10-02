"""Subworkflow resolver with a recursion guard (Master section 21).

Maps exact version-pinned refs (``name@vN``) to graphs. The plain
:meth:`resolve` is callable directly as the executor's ``subworkflow_resolver``
hook; :meth:`resolve_for_depth` additionally enforces the recursion limit,
raising :class:`SubworkflowDepthError` once the current depth reaches the
configured maximum.
"""

from __future__ import annotations

import re

from uap.graph import WorkflowGraph

__all__ = ["SubworkflowDepthError", "SubworkflowResolver"]

#: Exact version pin: lowercase name/digits/-/_ then ``@vN`` (section 2.4).
_REF_RE = re.compile(r"^[a-z0-9_-]+@v[0-9]+$")


class SubworkflowDepthError(Exception):
    """Raised when a subworkflow would exceed the recursion depth guard."""


class SubworkflowResolver:
    """Registers and resolves version-pinned subworkflow graphs."""

    def __init__(self, *, max_depth: int = 3) -> None:
        self.max_depth = max_depth
        self._graphs: dict[str, WorkflowGraph] = {}

    def register(self, ref: str, graph: WorkflowGraph) -> None:
        """Register ``graph`` under ``ref``; ``ref`` must be ``name@vN``."""
        if not _REF_RE.match(ref):
            raise ValueError(
                f"bad subworkflow ref {ref!r}: expected '^[a-z0-9_-]+@v[0-9]+$'"
            )
        self._graphs[ref] = graph

    def resolve(self, ref: str) -> WorkflowGraph:
        """Return the graph registered under ``ref`` or raise ``KeyError``."""
        graph = self._graphs.get(ref)
        if graph is None:
            known = sorted(self._graphs)
            raise KeyError(f"unknown subworkflow ref {ref!r}; registered: {known}")
        return graph

    def resolve_for_depth(self, ref: str, current_depth: int) -> WorkflowGraph:
        """Resolve ``ref`` while enforcing the recursion guard (section 21)."""
        if current_depth >= self.max_depth:
            raise SubworkflowDepthError(
                f"subworkflow depth {current_depth} >= max {self.max_depth} "
                f"resolving {ref!r} (recursion guard, section 21)"
            )
        return self.resolve(ref)
