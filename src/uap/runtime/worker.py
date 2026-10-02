"""Durable execution worker (Master sections 37, 38, 39, 46, 73).

A worker is a lease-holding loop over the durable queue in PostgreSQL. It knows
nothing about the graph engine or the UI: it claims an execution, asks the
:class:`~uap.runtime.service.ExecutionService` to run it, and repeats.

Claiming semantics (section 39: "Runtime scheduler determines placement"):

* **Queued** executions (``status = pending``) are claimable immediately.
* **Running** executions whose ``heartbeat_at`` is older than ``stale_after_s``
  are considered abandoned by a dead worker and are re-claimable - this is what
  makes an interrupted execution survive a process crash (section 38).
* A **fresh** running execution (recent heartbeat) is *not* claimable by a second
  worker, so two workers cannot both drive one execution.

Claiming is atomic and non-blocking: ``SELECT ... FOR UPDATE SKIP LOCKED`` inside
a short transaction, so N workers polling the same table never serialize on one
another and never double-claim a row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from uap.db.engine import session_scope
from uap.db.models.execution import Execution, ExecutionStatus

from .service import ExecutionService

__all__ = ["Worker"]

class Worker:
    """Lease-based durable execution worker (sections 37, 38, 39)."""

    def __init__(
        self,
        service: ExecutionService,
        *,
        worker_id: str | None = None,
        poll_interval_s: float = 1.0,
        heartbeat_interval_s: float = 10.0,
        stale_after_s: float = 60.0,
    ) -> None:
        self._service = service
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._poll_interval_s = float(poll_interval_s)
        self._heartbeat_interval_s = float(heartbeat_interval_s)
        self._stale_after_s = float(stale_after_s)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ------------------------------------------------------------------ #
    # Claiming
    # ------------------------------------------------------------------ #

    def claim_next(self) -> str | None:
        """Atomically claim the oldest claimable execution, or ``None``.

        Claimable = ``status = pending`` OR (``status = running`` AND the lease
        heartbeat is older than ``stale_after_s``, or missing). The claim sets
        ``locked_by = worker_id`` and ``heartbeat_at = now`` and commits before
        returning, so the row is visible as claimed to every other worker.
        """

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=self._stale_after_s)

        with session_scope(self._service.session_factory) as session:
            stmt = (
                select(Execution)
                .where(
                    or_(
                        Execution.status == ExecutionStatus.PENDING,
                        (
                            (Execution.status == ExecutionStatus.RUNNING)
                            & (
                                Execution.heartbeat_at.is_(None)
                                | (Execution.heartbeat_at < stale_before)
                            )
                        ),
                    )
                )
                .order_by(Execution.created_at.asc(), Execution.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None

            row.status = ExecutionStatus.RUNNING
            row.locked_by = self._worker_id
            row.heartbeat_at = now
            session.flush()
            return str(row.id)

    def claim_by_id(self, execution_id: str) -> str | None:
        """Claim ONE specific execution, or ``None`` when it is not claimable.

        Same lease rules as :meth:`claim_next` (pending, or running with a
        stale/missing heartbeat) but scoped to a single row. A synchronous
        caller that just created the row uses this so it runs ITS OWN work
        instead of draining the queue head.
        """

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=self._stale_after_s)
        try:
            eid = uuid.UUID(str(execution_id))
        except (ValueError, AttributeError):
            return None

        with session_scope(self._service.session_factory) as session:
            stmt = (
                select(Execution)
                .where(
                    Execution.id == eid,
                    or_(
                        Execution.status == ExecutionStatus.PENDING,
                        (
                            (Execution.status == ExecutionStatus.RUNNING)
                            & (
                                Execution.heartbeat_at.is_(None)
                                | (Execution.heartbeat_at < stale_before)
                            )
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None

            row.status = ExecutionStatus.RUNNING
            row.locked_by = self._worker_id
            row.heartbeat_at = now
            session.flush()
            return str(row.id)

    async def run_once(self) -> str | None:
        """Claim one execution and drive it to a terminal / paused state.

        Returns the execution id, or ``None`` when nothing was claimable.
        """

        execution_id = self.claim_next()
        if execution_id is None:
            return None

        return await self._drive(execution_id)

    async def run_specific(self, execution_id: str) -> str:
        """Drive THIS execution, not whichever one happens to be oldest.

        ``run_once`` claims the oldest pending row, which is correct for a
        background worker draining a queue and WRONG for a synchronous caller
        that just created a row and is waiting for its own result. Using
        ``run_once`` there ran the previous submission and returned a report
        attributed to the wrong task — verified: two back-to-back requests
        received each other's output (2026-10-03).

        The row must be claimed first so the lease semantics still hold; this
        only changes WHICH row is claimed.
        """

        claimed = self.claim_by_id(execution_id)
        if claimed is None:
            return "not_claimable"
        return await self._drive(execution_id)

    async def _drive(self, execution_id: str) -> str:
        """Run a claimed execution to completion, holding its lease."""

        # Renew the lease on a background cadence while the run is in flight.
        lease_stop = asyncio.Event()
        lease_task = asyncio.create_task(
            self._heartbeat_loop(execution_id, lease_stop)
        )
        try:
            await self._service.run_claimed(execution_id, self._worker_id)
        except Exception as exc:  # worker safety net: never leave a stuck lease
            self._service.fail(execution_id, f"{type(exc).__name__}: {exc}")
        finally:
            lease_stop.set()
            lease_task.cancel()
            try:
                await lease_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return execution_id

    async def _heartbeat_loop(self, execution_id: str, stop: asyncio.Event) -> None:
        """Refresh ``heartbeat_at`` every ``heartbeat_interval_s`` until stopped."""

        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._heartbeat_interval_s
                )
                return
            except asyncio.TimeoutError:
                self._service.heartbeat(execution_id, self._worker_id)

    # ------------------------------------------------------------------ #
    # Continuous loop
    # ------------------------------------------------------------------ #

    async def run_forever(self, *, stop_after: int | None = None) -> None:
        """Poll and process executions until stopped.

        With ``stop_after=N`` the loop returns after processing exactly ``N``
        executions (each successful :meth:`run_once`); idle polls do not count.
        Without it the loop runs until cancelled.
        """

        processed = 0
        while stop_after is None or processed < stop_after:
            execution_id = await self.run_once()
            if execution_id is None:
                if stop_after is not None:
                    # Nothing to do and a bounded run was requested: finish.
                    return
                await asyncio.sleep(self._poll_interval_s)
                continue
            processed += 1
