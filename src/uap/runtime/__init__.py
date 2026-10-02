"""Durable runtime: the execution control plane (Master sections 37, 38, 39, 46).

The UI connection must not own execution lifecycle (section 38). This package
separates three concerns:

* :class:`~uap.runtime.service.ExecutionService` - the durable control plane:
  enqueue, status, event replay (``events_since``), cooperative pause, approval
  resume and checkpoint forking. All state lives in PostgreSQL.
* :class:`~uap.runtime.worker.Worker` - the lease-holding loop that claims
  queued/stale executions and drives them through an injected graph executor.
* :class:`~uap.runtime.checkpoints.CheckpointStore` - durable ``(seq, state)``
  snapshots so a restarted worker resumes without re-running committed nodes.

The graph executor (``uap.execution``) and the canonical events (``uap.events``)
are imported lazily, so this package imports even while those sibling waves are
still landing.

Usage::

    from uap.runtime import ExecutionService, Worker, CheckpointStore

    service = ExecutionService(session_factory, graph_resolver=resolve,
                               node_runtime=runtime)
    execution_id = service.enqueue(workflow_ref="recon@v1", inputs={"target": "x"})
    await Worker(service).run_forever(stop_after=1)
    print(service.status(execution_id))
"""

from __future__ import annotations

from .checkpoints import CHECKPOINTS_AVAILABLE, CheckpointStore
from .service import STATUS_MAPPING, ExecutionService
from .worker import Worker

__all__ = [
    "CheckpointStore",
    "CHECKPOINTS_AVAILABLE",
    "ExecutionService",
    "STATUS_MAPPING",
    "Worker",
]
