"""Nested subworkflows (Master section 21).

A :class:`SubworkflowResolver` maps exact version-pinned refs (``name@vN``) to
:class:`~uap.graph.model.WorkflowGraph` instances and plugs straight into
:meth:`GraphExecutor.execute(..., subworkflow_resolver=...)`. It also exposes a
depth-aware lookup so a caller can enforce the recursion guard (section 21)
before descending into a nested graph.
"""

from uap.subworkflows.resolver import SubworkflowDepthError, SubworkflowResolver

__all__ = ["SubworkflowDepthError", "SubworkflowResolver"]
