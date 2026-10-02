"""Results must be attributed to the run that requested them.

Regression for a real defect found on 2026-10-03: two back-to-back requests
received each other's output. The synchronous slice path did:

    execution_id = service.enqueue(...)   # creates a NEW row
    await Worker(service).run_once()      # claims the OLDEST pending row
    status = service.status(execution_id) # reads back MY id

``run_once`` drains the queue head, so it ran the PREVIOUS submission and the
caller read the result under its own id. In a multi-user deployment that is a
cross-user data leak: the report stored under your task id belongs to whoever
submitted before you.

The fix is ``Worker.run_specific`` / ``claim_by_id``: a synchronous caller runs
the row it just created. ``run_once`` keeps its queue-draining behaviour, which
is correct for a background worker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from uap.db.engine import get_session_factory, session_scope
from uap.db.models.execution import Execution, ExecutionStatus
from uap.runtime.service import ExecutionService
from uap.runtime.worker import Worker


def _service() -> ExecutionService:
    def _resolver(ref: str):
        # enqueue() calls this to validate the ref before inserting.
        return None

    class _NodeRuntime:
        async def run_node(self, *args, **kwargs):  # pragma: no cover - unused
            return {}

    return ExecutionService(
        get_session_factory(),
        graph_resolver=_resolver,
        node_runtime=_NodeRuntime(),
    )


def _enqueue(service: ExecutionService, correlation_id: uuid.UUID) -> str:
    """Enqueue a real execution row, reusing an existing workflow version."""
    with session_scope(service.session_factory) as session:
        from uap.db.models.definitions import WorkflowVersion

        wv = session.execute(select(WorkflowVersion).limit(1)).scalar_one_or_none()
        if wv is None:
            raise RuntimeError("no workflow_version row to attach to")
        return service.enqueue(
            workflow_ref="research-slice",
            inputs={"value": "probe"},
            correlation_id=correlation_id,
            workspace_id=None,
        )


def test_claim_by_id_claims_the_requested_row_not_the_oldest() -> None:
    """The decisive test: two rows, claim the NEWER one by id.

    With the old behaviour (``claim_next``) this returns the OLDER row.
    """
    service = _service()

    older = _enqueue(service, uuid.uuid4())
    newer = _enqueue(service, uuid.uuid4())

    worker = Worker(service, worker_id="claim-by-id-test")
    claimed = worker.claim_by_id(newer)

    assert claimed == newer, (
        "claim_by_id must claim the requested row; "
        f"asked for {newer}, got {claimed}"
    )

    # The older row must still be untouched and claimable — proving we did not
    # accidentally drain the queue head instead.
    with session_scope(service.session_factory) as session:
        row = session.get(Execution, uuid.UUID(older))
        assert row is not None
        assert row.status is ExecutionStatus.PENDING, (
            f"the older row was claimed as a side effect: {row.status}"
        )
        assert row.locked_by is None


def test_claim_by_id_refuses_an_already_claimed_row() -> None:
    """A row another worker holds must not be claimable a second time."""
    service = _service()
    eid = _enqueue(service, uuid.uuid4())

    first = Worker(service, worker_id="w1").claim_by_id(eid)
    assert first == eid

    second = Worker(service, worker_id="w2").claim_by_id(eid)
    assert second is None, "a freshly claimed (running, fresh heartbeat) row was re-claimed"


def test_claim_by_id_returns_none_for_unknown_id() -> None:
    service = _service()
    assert Worker(service, worker_id="w").claim_by_id(str(uuid.uuid4())) is None


def test_claim_by_id_returns_none_for_malformed_id() -> None:
    """A non-UUID must not raise; it is simply not claimable."""
    service = _service()
    assert Worker(service, worker_id="w").claim_by_id("not-a-uuid") is None


def test_run_once_still_drains_the_queue_head() -> None:
    """Background-worker behaviour must be preserved, not broken by the fix.

    This asserts ORDERING, not a specific row: other tests in the session may
    have left claimable rows behind, and ``claim_next`` legitimately takes the
    oldest of ALL of them. So the check is that the row we created FIRST is
    claimed before the row we created SECOND.
    """
    service = _service()
    older = _enqueue(service, uuid.uuid4())
    newer = _enqueue(service, uuid.uuid4())

    worker = Worker(service, worker_id="drain-test")
    claimed = worker.claim_next()
    assert claimed is not None

    # Whichever rows exist, `older` must be claimed no later than `newer`.
    with session_scope(service.session_factory) as session:
        older_row = session.get(Execution, uuid.UUID(older))
        newer_row = session.get(Execution, uuid.UUID(newer))
        older_claimed = older_row.status is not ExecutionStatus.PENDING
        newer_claimed = newer_row.status is not ExecutionStatus.PENDING

    if older_claimed and not newer_claimed:
        return  # correct: oldest first
    assert not newer_claimed, (
        "claim_next claimed the NEWER row while the OLDER row was still pending; "
        "queue draining must be oldest-first"
    )
