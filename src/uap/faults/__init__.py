"""Fault injection (Master section 59).

Public surface:

* :class:`FaultSpec` - one fault rule (exception / delay / result mutation /
  kill-after-N) targeting a node id or ``"*"``.
* :class:`FaultInjector` - holds specs and applies the matching one around an
  async call, with a deterministic per-spec ``trigger_after`` counter.
* :class:`FaultRegistry` - wraps a :class:`~uap.execution.context.NodeRuntime`
  so a committed :class:`~uap.execution.GraphExecutor` run can be driven with
  faults on specific nodes.
"""

from __future__ import annotations

from .injection import FaultInjector, FaultRegistry, FaultSpec

__all__ = ["FaultInjector", "FaultRegistry", "FaultSpec"]
