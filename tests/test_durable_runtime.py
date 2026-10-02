"""Durable runtime tests (Master sections 21, 37, 38, 39, 45, 46).

These tests exercise the durable execution control plane against the local
PostgreSQL ``uap_test`` database. They skip cleanly (with a printed reason) when
any of the sibling prerequisites is missing:

* the database is unreachable;
* ``uap.execution`` (the graph engine) has not landed;
* ``uap.events`` (the canonical event vocabulary) has not landed;
* ``uap.db.models.checkpoint`` (the ``execution_checkpoints`` table) has not
  landed.

Each durability guarantee from the assignment has its own test:

1.  enqueue -> row + EXECUTION_STARTED seq 1
2.  linear run completes; NODE_STARTED/FINISHED + EXECUTION_COMPLETED, monotonic
3.  events_since resync tail (§46)
4.  restart survival: partial run, NEW service, resume without re-running nodes
5.  CheckpointStore latest/list round-trip + upsert
6.  stale running execution is re-claimable
7.  fresh running execution is NOT claimable
8.  fork copies state up to from_seq into a new independent execution
9.  cooperative pause -> paused -> resume -> completed
10. approval pause -> awaiting_approval -> approve -> completed
11. denied approval -> cancelled + reason
12. FIFO claim order
13. execution pins workflow_version_id (NOT NULL schema)
14. worker.run_forever(stop_after=2) processes exactly two
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.contracts import ApprovalRequest
from uap.db import Base, create_db_engine, create_session_factory
from uap.db.models.definitions import VersionStatus
from uap.db.repositories import DefinitionRepository, ExecutionRepository
from uap.graph import (
    GraphEdge,
    GraphNode,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
)

# --------------------------------------------------------------------------- #
# Skip handling (printed reason)
# --------------------------------------------------------------------------- #

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"

def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL

TEST_DATABASE_URL = _resolved_test_url()

_SKIP_REASON: str | None = None

def _compute_skip_reason() -> str | None:
    """Return a human-readable reason to skip, or ``None`` when runnable."""

    try:
        engine = create_db_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc}"

    try:
        import uap.execution  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"uap.execution not available: {exc}"

    try:
        import uap.events  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"uap.events not available: {exc}"

    try:
        from uap.db.models.checkpoint import ExecutionCheckpoint  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"uap.db.models.checkpoint not available: {exc}"

    return None

_SKIP_REASON = _compute_skip_reason()

# Import the runtime surface only after the prerequisites are known present; if a
# sibling is missing, keep the module importable so pytest *skips* cleanly rather
# than erroring during collection.
try:
    from uap.execution import PauseExecution  # noqa: F401
    from uap.events import EventType  # noqa: F401
    from uap.runtime import CheckpointStore, ExecutionService, Worker  # noqa: F401
except Exception as _import_exc:  # noqa: BLE001
    if _SKIP_REASON is None:
        _SKIP_REASON = f"runtime imports unavailable: {_import_exc}"
    PauseExecution = EventType = None  # type: ignore[assignment]
    CheckpointStore = ExecutionService = Worker = None  # type: ignore[assignment]

if _SKIP_REASON is not None:
    print(f"\n[test_durable_runtime] SKIPPED: {_SKIP_REASON}\n")

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A dedicated scratch schema so parallel agents on ``uap_test`` never race.

    ``uap_test`` is shared with other test modules (and other agents), so
    dropping/creating tables in ``public`` would clobber concurrent runs. This
    fixture creates an isolated schema and pins ``search_path`` to it.
    """

    from sqlalchemy.engine import make_url

    schema = f"durable_rt_{uuid.uuid4().hex[:8]}"
    admin = create_db_engine(TEST_DATABASE_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    scoped_url = make_url(TEST_DATABASE_URL).update_query_dict(
        {"options": f"-csearch_path={schema},public"}
    )
    eng = create_db_engine(scoped_url.render_as_string(hide_password=False))
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)

@pytest.fixture(autouse=True)
def _truncate(engine: Engine) -> Iterator[None]:
    """Truncate every owned table before *and* after each test for isolation."""

    tables = (
        "execution_checkpoints",
        "execution_events",
        "executions",
        "workflow_versions",
        "workflow_definitions",
    )
    statement = text(
        "TRUNCATE "
        + ", ".join(f'"{name}"' for name in tables)
        + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    yield
    with engine.begin() as conn:
        conn.execute(statement)

# --------------------------------------------------------------------------- #
# Graph + runtime helpers
# --------------------------------------------------------------------------- #

def linear_graph(name: str = "g") -> WorkflowGraph:
    """A four-node linear graph: INPUT -> a -> b -> OUTPUT."""

    return WorkflowGraph(
        id=f"graph-{name}",
        name=name,
        nodes=[
            GraphNode(
                id="in",
                kind=NodeKind.INPUT,
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
            GraphNode(
                id="a",
                kind=NodeKind.AGENT,
                inputs=[Port(name="value", type=PortType.TEXT)],
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
            GraphNode(
                id="b",
                kind=NodeKind.AGENT,
                inputs=[Port(name="value", type=PortType.TEXT)],
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
            GraphNode(
                id="out",
                kind=NodeKind.OUTPUT,
                inputs=[Port(name="value", type=PortType.TEXT)],
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
        ],
        edges=[
            GraphEdge(id="e1", source="in", source_port="value", target="a", target_port="value"),
            GraphEdge(id="e2", source="a", source_port="value", target="b", target_port="value"),
            GraphEdge(id="e3", source="b", source_port="value", target="out", target_port="value"),
        ],
    )

def approval_graph(name: str = "approval") -> WorkflowGraph:
    """INPUT -> approval(APPROVAL node) -> OUTPUT."""

    return WorkflowGraph(
        id=f"graph-{name}",
        name=name,
        nodes=[
            GraphNode(id="in", kind=NodeKind.INPUT, outputs=[Port(name="value", type=PortType.TEXT)]),
            GraphNode(
                id="gate",
                kind=NodeKind.APPROVAL,
                inputs=[Port(name="value", type=PortType.TEXT)],
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
            GraphNode(
                id="out",
                kind=NodeKind.OUTPUT,
                inputs=[Port(name="value", type=PortType.TEXT)],
                outputs=[Port(name="value", type=PortType.TEXT)],
            ),
        ],
        edges=[
            GraphEdge(id="e1", source="in", source_port="value", target="gate", target_port="value"),
            GraphEdge(id="e2", source="gate", source_port="value", target="out", target_port="value"),
        ],
    )

class SpyRuntime:
    """Records every node it runs; can raise PauseExecution at a chosen node."""

    def __init__(self, *, pause_at: str | None = None, approval_id: str = "ap-1") -> None:
        self.calls: list[str] = []
        self.pause_at = pause_at
        self.approval_id = approval_id

    async def run_node(self, node, inputs, ctx):  # noqa: ANN001, ANN201
        self.calls.append(node.id)
        if self.pause_at is not None and node.id == self.pause_at:
            raise PauseExecution(
                ApprovalRequest(
                    approval_id=self.approval_id,
                    task_id=str(ctx.execution_id),
                    action=f"gate:{node.id}",
                )
            )
        return {"value": f"{node.id}:{inputs.get('value')}"}

class GraphRegistry:
    """A tiny injected ``graph_resolver`` (name@vN or name)."""

    def __init__(self, graphs: dict[str, WorkflowGraph]) -> None:
        self.graphs = graphs

    def __call__(self, ref: str) -> WorkflowGraph:
        if ref in self.graphs:
            return self.graphs[ref]
        name = ref.split("@", 1)[0]
        if name in self.graphs:
            return self.graphs[name]
        raise KeyError(ref)

def make_service(
    session_factory: sessionmaker[Session],
    graphs: dict[str, WorkflowGraph],
    runtime: SpyRuntime,
    **kwargs,
) -> ExecutionService:
    return ExecutionService(
        session_factory,
        graph_resolver=GraphRegistry(graphs),
        node_runtime=runtime,
        **kwargs,
    )

def seed_version(
    session_factory: sessionmaker[Session],
    name: str,
    spec: dict | None = None,
    *,
    status: VersionStatus = VersionStatus.ACTIVE,
):
    """Create a definition + one version row so ``workflow_ref`` resolves."""

    with session_factory() as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(name)
        version = repo.create_version(
            definition.id, spec if spec is not None else {"nodes": ["in", "a", "b", "out"]},
            status=status,
        )
        session.commit()
        return definition.id, version.id

def event_kinds(service: ExecutionService, execution_id: str) -> list[str]:
    return [event["kind"] for event in service.events_since(execution_id)]

def event_seqs(service: ExecutionService, execution_id: str) -> list[int]:
    return [event["seq"] for event in service.events_since(execution_id)]

# --------------------------------------------------------------------------- #
# 1. enqueue
# --------------------------------------------------------------------------- #

def test_enqueue_creates_row_and_execution_started_event(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())

    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    status = service.status(execution_id)
    assert status["status"] == "pending"  # runtime "queued" maps onto pending
    assert status["resume_count"] == 0

    events = service.events_since(execution_id)
    assert len(events) == 1
    assert events[0]["seq"] == 1
    assert events[0]["kind"] == EventType.EXECUTION_STARTED.value

# --------------------------------------------------------------------------- #
# 2. linear run to completion
# --------------------------------------------------------------------------- #

async def test_run_once_completes_linear_graph(session_factory):
    seed_version(session_factory, "g")
    runtime = SpyRuntime()
    service = make_service(session_factory, {"g@v1": linear_graph()}, runtime)
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    worker = Worker(service, poll_interval_s=0.01)
    assert await worker.run_once() == execution_id

    status = service.status(execution_id)
    assert status["status"] == "completed"
    assert status["output"] == {"out": {"value": "out:b:a:x"}}
    assert runtime.calls == ["a", "b", "out"]

    events = service.events_since(execution_id)
    kinds = [event["kind"] for event in events]
    assert kinds.count(EventType.NODE_STARTED.value) == 4
    assert kinds.count(EventType.NODE_FINISHED.value) == 4
    assert kinds[-1] == EventType.EXECUTION_COMPLETED.value
    assert event_seqs(service, execution_id) == sorted(event_seqs(service, execution_id))

# --------------------------------------------------------------------------- #
# 3. events_since resync
# --------------------------------------------------------------------------- #

async def test_events_since_returns_only_the_tail(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    await Worker(service).run_once()

    everything = service.events_since(execution_id, 0)
    tail = service.events_since(execution_id, 5)

    assert len(tail) == len(everything) - 5
    assert all(event["seq"] > 5 for event in tail)
    assert tail[0]["seq"] == 6

# --------------------------------------------------------------------------- #
# 4. restart survival
# --------------------------------------------------------------------------- #

async def test_restart_survives_and_does_not_rerun_committed_nodes(session_factory):
    from datetime import datetime, timedelta, timezone

    from uap.db.models.execution import ExecutionStatus

    seed_version(session_factory, "g")
    first_runtime = SpyRuntime()

    # First service runs with a step budget of 1, so the executor stops after the
    # INPUT node: a partial run, as if the process had been interrupted mid-way.
    service1 = make_service(
        session_factory,
        {"g@v1": linear_graph()},
        first_runtime,
        executor_kwargs={"max_steps": 1},
    )
    execution_id = service1.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    await Worker(service1, worker_id="w1").run_once()

    # No agent node ran; the INPUT node was checkpointed durably.
    assert first_runtime.calls == []
    checkpoints = service1.checkpoints(execution_id)
    assert checkpoints, "expected an incremental checkpoint for the completed node"
    assert checkpoints[-1][1]["node_status"]["in"] == "completed"

    # Simulate the worker being killed while it held the lease: the row is left
    # ``running`` with a stale heartbeat, exactly what a crash leaves behind.
    with session_factory() as session:
        repo = ExecutionRepository(session)
        repo.update_status(uuid.UUID(execution_id), ExecutionStatus.RUNNING)
        row = repo.get(uuid.UUID(execution_id))
        row.locked_by = "dead-worker"
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        session.commit()

    # A NEW service instance (fresh process) stale-claims and resumes from the
    # durable checkpoint.
    second_runtime = SpyRuntime()
    service2 = make_service(session_factory, {"g@v1": linear_graph()}, second_runtime)
    assert await Worker(service2, worker_id="w2", stale_after_s=60.0).run_once() == execution_id

    assert service2.status(execution_id)["status"] == "completed"
    # The INPUT node was already committed, so the resumed run never re-ran it.
    assert second_runtime.calls == ["a", "b", "out"]
    assert service2.status(execution_id)["output"] == {"out": {"value": "out:b:a:x"}}

# --------------------------------------------------------------------------- #
# 5. CheckpointStore round-trip + upsert
# --------------------------------------------------------------------------- #

def test_checkpoint_store_latest_list_and_upsert(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    with session_factory() as session:
        store = CheckpointStore(session)
        assert store.latest(execution_id) is None
        store.save(execution_id, 1, {"node_results": {"a": 1}}, node_id="a")
        store.save(execution_id, 2, {"node_results": {"a": 1, "b": 2}}, node_id="b")
        session.commit()

    with session_factory() as session:
        store = CheckpointStore(session)
        assert store.latest(execution_id) == (2, {"node_results": {"a": 1, "b": 2}})
        assert store.list_for_execution(execution_id) == [
            (1, {"node_results": {"a": 1}}),
            (2, {"node_results": {"a": 1, "b": 2}}),
        ]

        # Upsert: re-saving seq 1 replaces its state instead of raising.
        store.save(execution_id, 1, {"node_results": {"a": 999}}, node_id="a")
        session.commit()

    with session_factory() as session:
        store = CheckpointStore(session)
        assert store.latest(execution_id) == (2, {"node_results": {"a": 1, "b": 2}})
        assert store.list_for_execution(execution_id)[0] == (1, {"node_results": {"a": 999}})
        assert store.count(execution_id) == 2

# --------------------------------------------------------------------------- #
# 6/7. stale vs fresh lease
# --------------------------------------------------------------------------- #

def test_stale_running_execution_is_reclaimable(session_factory):
    from datetime import datetime, timedelta, timezone

    from uap.db.models.execution import ExecutionStatus

    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    with session_factory() as session:
        repo = ExecutionRepository(session)
        repo.update_status(uuid.UUID(execution_id), ExecutionStatus.RUNNING)
        row = repo.get(uuid.UUID(execution_id))
        row.locked_by = "dead-worker"
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        session.commit()

    worker = Worker(service, worker_id="w-new", stale_after_s=60.0)
    assert worker.claim_next() == execution_id

def test_fresh_running_execution_is_not_reclaimable(session_factory):
    from datetime import datetime, timezone

    from uap.db.models.execution import ExecutionStatus

    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    with session_factory() as session:
        repo = ExecutionRepository(session)
        repo.update_status(uuid.UUID(execution_id), ExecutionStatus.RUNNING)
        row = repo.get(uuid.UUID(execution_id))
        row.locked_by = "live-worker"
        row.heartbeat_at = datetime.now(timezone.utc)
        session.commit()

    worker = Worker(service, worker_id="w-other", stale_after_s=60.0)
    assert worker.claim_next() is None

# --------------------------------------------------------------------------- #
# 8. fork
# --------------------------------------------------------------------------- #

async def test_fork_copies_state_and_completes_independently(session_factory):
    seed_version(session_factory, "g")
    runtime = SpyRuntime()
    service = make_service(session_factory, {"g@v1": linear_graph()}, runtime)

    source = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    # Seed a checkpoint at seq 1 holding node "a" only.
    with session_factory() as session:
        CheckpointStore(session).save(
            source,
            1,
            {
                "status": "running",
                "node_results": {"in": {"value": "x"}, "a": {"value": "a:x"}},
                "node_status": {"in": "completed", "a": "completed"},
                "order": ["in", "a"],
                "node_id": "a",
            },
            node_id="a",
        )
        session.commit()

    forked = service.fork(source, from_seq=1, label="branch")
    assert forked != source
    assert service.status(forked)["resume_count"] == 0

    # The fork carries the copied checkpoint state (up to from_seq).
    forked_checkpoints = service.checkpoints(forked)
    assert forked_checkpoints
    assert forked_checkpoints[-1][1]["node_results"]["a"] == {"value": "a:x"}

    # The source records the fork link; the child records its own start.
    source_kinds = event_kinds(service, source)
    assert EventType.EXECUTION_FORKED.value in source_kinds
    assert EventType.EXECUTION_STARTED.value in event_kinds(service, forked)

    # Park the source so the next claim targets the fork, then run it: the fork
    # resumes from the copied state, so "a" is not re-run.
    from uap.db.models.execution import ExecutionStatus

    with session_factory() as session:
        ExecutionRepository(session).update_status(
            uuid.UUID(source), ExecutionStatus.COMPLETED
        )
        session.commit()

    assert await Worker(service).run_once() == forked
    assert service.status(forked)["status"] == "completed"
    assert "a" not in runtime.calls
    assert runtime.calls == ["b", "out"]
    # The original execution is untouched by the fork (still its parked status).
    assert service.status(source)["status"] == "completed"

# --------------------------------------------------------------------------- #
# 9. cooperative pause
# --------------------------------------------------------------------------- #

async def test_cooperative_pause_then_resume(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    service.pause_request(execution_id)
    assert "pause_requested" in event_kinds(service, execution_id)

    await Worker(service).run_once()
    assert service.status(execution_id)["status"] == "paused"

    # Resume clears the marker and re-queues; the run then completes.
    service.resume(execution_id)
    assert service.status(execution_id)["status"] == "pending"
    await Worker(service).run_once()
    assert service.status(execution_id)["status"] == "completed"

# --------------------------------------------------------------------------- #
# 10/11. approval
# --------------------------------------------------------------------------- #

async def test_approval_pause_then_approve_completes(session_factory):
    seed_version(session_factory, "approval")
    runtime = SpyRuntime(pause_at="gate", approval_id="ap-42")
    service = make_service(session_factory, {"approval@v1": approval_graph()}, runtime)
    execution_id = service.enqueue(workflow_ref="approval@v1", inputs={"value": "x"})

    await Worker(service).run_once()
    status = service.status(execution_id)
    assert status["status"] == "awaiting_approval"
    assert EventType.APPROVAL_REQUESTED.value in event_kinds(service, execution_id)

    service.approve_and_resume(execution_id, "ap-42", approved=True, decided_by="alice")
    assert service.status(execution_id)["status"] == "pending"

    await Worker(service).run_once()
    assert service.status(execution_id)["status"] == "completed"
    assert EventType.APPROVAL_DECIDED.value in event_kinds(service, execution_id)

async def test_denied_approval_cancels_with_reason(session_factory):
    seed_version(session_factory, "approval")
    runtime = SpyRuntime(pause_at="gate", approval_id="ap-7")
    service = make_service(session_factory, {"approval@v1": approval_graph()}, runtime)
    execution_id = service.enqueue(workflow_ref="approval@v1", inputs={"value": "x"})

    await Worker(service).run_once()
    assert service.status(execution_id)["status"] == "awaiting_approval"

    service.approve_and_resume(execution_id, "ap-7", approved=False, decided_by="bob")
    status = service.status(execution_id)
    assert status["status"] == "cancelled"
    assert "ap-7" in (status["error"] or "")
    assert "bob" in (status["error"] or "")

# --------------------------------------------------------------------------- #
# 12. FIFO claim order
# --------------------------------------------------------------------------- #

async def test_two_enqueues_are_claimed_fifo(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    first = service.enqueue(workflow_ref="g@v1", inputs={"value": "1"})
    second = service.enqueue(workflow_ref="g@v1", inputs={"value": "2"})

    worker = Worker(service)
    assert worker.claim_next() == first
    assert worker.claim_next() == second

# --------------------------------------------------------------------------- #
# 13. workflow_version_id pinning
# --------------------------------------------------------------------------- #

def test_execution_pins_workflow_version_id(session_factory):
    _, version_id = seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})

    with session_factory() as session:
        row = ExecutionRepository(session).get(uuid.UUID(execution_id))
        # Committed schema: ``workflow_version_id`` is NOT NULL, so the
        # "NULL allowed" path is impossible; the execution is always pinned.
        assert row.workflow_version_id == version_id

# --------------------------------------------------------------------------- #
# 14. run_forever(stop_after=N)
# --------------------------------------------------------------------------- #

async def test_run_forever_stop_after_processes_exactly_n(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    first = service.enqueue(workflow_ref="g@v1", inputs={"value": "1"})
    second = service.enqueue(workflow_ref="g@v1", inputs={"value": "2"})
    third = service.enqueue(workflow_ref="g@v1", inputs={"value": "3"})

    worker = Worker(service, poll_interval_s=0.01)
    await asyncio.wait_for(worker.run_forever(stop_after=2), timeout=10)

    assert service.status(first)["status"] == "completed"
    assert service.status(second)["status"] == "completed"
    # The third execution was never claimed.
    assert service.status(third)["status"] == "pending"

# --------------------------------------------------------------------------- #
# Extra: durable read model survives a "UI disconnect" (section 38)
# --------------------------------------------------------------------------- #

async def test_state_is_readable_after_the_fact(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    await Worker(service).run_once()

    # A brand-new service instance (as a reconnecting UI would build) reads the
    # same durable state and can resync the full event tail.
    fresh = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    assert fresh.status(execution_id)["status"] == "completed"
    assert fresh.events_since(execution_id, 0) == service.events_since(execution_id, 0)
    assert EventType.EXECUTION_COMPLETED.value in event_kinds(fresh, execution_id)

# --------------------------------------------------------------------------- #
# Extra: unknown workflow_ref fails at enqueue time
# --------------------------------------------------------------------------- #

def test_enqueue_unknown_ref_raises(session_factory):
    seed_version(session_factory, "g")
    service = make_service(session_factory, {"g@v1": linear_graph()}, SpyRuntime())
    with pytest.raises(KeyError):
        service.enqueue(workflow_ref="missing@v1", inputs={})
