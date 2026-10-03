"""Fault injection + execution replay tests (Master sections 59, 61).

Pure-async fault-injector tests run without a database. The executor-integration
and replay tests need the local PostgreSQL ``uap_test`` database and the sibling
graph/runtime/events packages; they skip cleanly (with a printed reason) when a
prerequisite is missing, mirroring ``tests/test_durable_runtime.py``.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from uap.faults import FaultInjector, FaultRegistry, FaultSpec

# --------------------------------------------------------------------------- #
# Part A: fault injector unit tests (no database)
# --------------------------------------------------------------------------- #


async def _ok(*_args, **_kwargs) -> dict:
    return {"value": "ok"}


# 1. exception fault raises the mapped type
async def test_exception_fault_raises_mapped_type():
    injector = FaultInjector([
        FaultSpec(target="n", kind="exception", exception_type="ConnectionError", message="boom"),
    ])
    with pytest.raises(ConnectionError, match="boom"):
        await injector.apply("n", _ok)


# 2. trigger_after fires only on the Nth matching call
async def test_trigger_after_fires_only_on_nth_call():
    injector = FaultInjector([
        FaultSpec(target="n", kind="exception", message="third", trigger_after=3),
    ])
    assert await injector.apply("n", _ok) == {"value": "ok"}  # call 1
    assert await injector.apply("n", _ok) == {"value": "ok"}  # call 2
    with pytest.raises(RuntimeError, match="third"):
        await injector.apply("n", _ok)  # call 3 fires
    assert await injector.apply("n", _ok) == {"value": "ok"}  # call 4 clean


# 3. delay fault awaits at least delay_s
async def test_delay_fault_awaits():
    injector = FaultInjector([FaultSpec(target="n", kind="delay", delay_s=0.05)])
    start = time.monotonic()
    result = await injector.apply("n", _ok)
    elapsed = time.monotonic() - start
    assert result == {"value": "ok"}
    assert elapsed >= 0.05


# 4. result_mutation stamps __injected__
async def test_result_mutation_adds_injected_marker():
    spec = FaultSpec(target="n", kind="result_mutation")
    injector = FaultInjector([spec])
    result = await injector.apply("n", _ok)
    assert result["value"] == "ok"
    assert result["__injected__"] == spec.fault_id

    async def _list(*_a, **_k):
        return [1, 2]

    injector2 = FaultInjector([FaultSpec(target="n", kind="result_mutation", fault_id="fid")])
    assert await injector2.apply("n", _list) == [1, 2, {"__injected__": "fid"}]


# 5. kill_after_n raises RuntimeError after N matching calls
async def test_kill_after_n_raises_runtimeerror():
    injector = FaultInjector([
        FaultSpec(target="n", kind="kill_after_n", trigger_after=2),
    ])
    assert await injector.apply("n", _ok) == {"value": "ok"}  # call 1
    with pytest.raises(RuntimeError, match="worker killed by fault injection"):
        await injector.apply("n", _ok)  # call 2 kills


# 6. clear() disables all faults
async def test_clear_disables_faults():
    injector = FaultInjector([FaultSpec(target="*", kind="exception")])
    injector.clear()
    assert await injector.apply("anything", _ok) == {"value": "ok"}


# wildcard target matches any node
async def test_wildcard_target_matches_any_node():
    injector = FaultInjector([FaultSpec(target="*", kind="exception", message="wild")])
    with pytest.raises(RuntimeError, match="wild"):
        await injector.apply("whatever", _ok)


# non-matching target is a no-op
async def test_non_matching_target_passes_through():
    injector = FaultInjector([FaultSpec(target="other", kind="exception")])
    assert await injector.apply("n", _ok) == {"value": "ok"}


# --------------------------------------------------------------------------- #
# Part B: executor + replay integration (database required)
# --------------------------------------------------------------------------- #

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


TEST_DATABASE_URL = _resolved_test_url()

_SKIP_REASON: str | None = None


def _compute_skip_reason() -> str | None:
    try:
        from sqlalchemy import text

        from uap.db import create_db_engine

        engine = create_db_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc}"

    for module in ("uap.execution", "uap.events"):
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001
            return f"{module} not available: {exc}"

    try:
        from uap.db.models.checkpoint import ExecutionCheckpoint  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"uap.db.models.checkpoint not available: {exc}"

    return None


_SKIP_REASON = _compute_skip_reason()

if _SKIP_REASON is not None:
    print(f"\n[test_faults_replay] SKIPPED (integration): {_SKIP_REASON}\n")

_db = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# --- fixtures (isolated scratch schema, mirrors test_durable_runtime) ------- #


@pytest.fixture(scope="module")
def engine(isolated_engine):
    """The module's isolated schema (migrations applied by ``tests/conftest.py``)."""
    return isolated_engine


@pytest.fixture()
def session_factory(engine):
    from uap.db import create_session_factory

    return create_session_factory(engine)


@pytest.fixture(autouse=True)
def _truncate(engine):
    from sqlalchemy import text

    tables = (
        "execution_checkpoints",
        "execution_events",
        "executions",
        "workflow_versions",
        "workflow_definitions",
    )
    statement = text(
        "TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    yield
    with engine.begin() as conn:
        conn.execute(statement)


# --- helpers ---------------------------------------------------------------- #


def _linear_graph(name: str = "g"):
    from uap.graph import GraphEdge, GraphNode, NodeKind, Port, PortType, WorkflowGraph

    return WorkflowGraph(
        id=f"graph-{name}",
        name=name,
        nodes=[
            GraphNode(id="in", kind=NodeKind.INPUT, outputs=[Port(name="value", type=PortType.TEXT)]),
            GraphNode(
                id="a",
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
            GraphEdge(id="e2", source="a", source_port="value", target="out", target_port="value"),
        ],
    )


class _Spy:
    """Minimal NodeRuntime: echoes ``node.id:input``."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_node(self, node, inputs, ctx):
        self.calls.append(node.id)
        return {"value": f"{node.id}:{inputs.get('value')}"}


def _graph_resolver(graph):
    def resolve(ref: str):
        return graph

    return resolve


def _make_service(session_factory, graph, runtime, **kwargs):
    from uap.runtime import ExecutionService

    return ExecutionService(
        session_factory,
        graph_resolver=_graph_resolver(graph),
        node_runtime=runtime,
        **kwargs,
    )


def _seed_version(session_factory, name: str):
    from uap.db.models.definitions import VersionStatus
    from uap.db.repositories import DefinitionRepository

    with session_factory() as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(name)
        repo.create_version(definition.id, {"nodes": ["in", "a", "out"]}, status=VersionStatus.ACTIVE)
        session.commit()


# 7. FaultRegistry.wrap_node_runtime injects into a GraphExecutor run
@_db
async def test_wrap_node_runtime_injects_into_executor():
    from uap.execution import GraphExecutor

    graph = _linear_graph()
    registry = FaultRegistry()
    registry.add(FaultSpec(target="a", kind="exception", message="injected into a"))

    wrapped = registry.wrap_node_runtime(_Spy())
    executor = GraphExecutor(wrapped)
    result = await executor.execute(graph, {"value": "x"}, execution_id="exec-1")

    assert result.status == "failed"
    assert result.node_status.get("a") == "failed"
    assert "injected into a" in (result.error or "")


# 8. ReplayEngine.build_plan unknown execution -> KeyError
@_db
def test_build_plan_unknown_execution_raises_keyerror(session_factory):
    from uap.replay import ReplayEngine

    graph = _linear_graph()
    service = _make_service(session_factory, graph, _Spy())
    engine = ReplayEngine(service)
    with pytest.raises(KeyError):
        engine.build_plan(str(uuid.uuid4()))


# 9. node_results_from_events reconstructs from a real run's events
@_db
async def test_node_results_reconstructed_from_real_run(session_factory):
    from uap.replay import ReplayEngine
    from uap.runtime import Worker

    graph = _linear_graph()
    _seed_version(session_factory, "g")
    service = _make_service(session_factory, graph, _Spy())
    execution_id = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    assert await Worker(service, poll_interval_s=0.01).run_once() == execution_id
    assert service.status(execution_id)["status"] == "completed"

    engine = ReplayEngine(service)
    reconstructed = engine.node_results_from_events(execution_id)
    # Every node that finished is present (payload carries no outputs, so each
    # maps to an empty dict - the reconstruction is driven by NODE_FINISHED).
    assert set(reconstructed) == {"in", "a", "out"}


# 10. create_replay enqueues a new execution carrying __replay__ provenance
@_db
async def test_create_replay_enqueues_new_execution(session_factory):
    from uap.replay import REPLAY_KEY, ReplayEngine
    from uap.runtime import Worker

    graph = _linear_graph()
    _seed_version(session_factory, "g")
    service = _make_service(session_factory, graph, _Spy())
    source = service.enqueue(workflow_ref="g@v1", inputs={"value": "x"})
    await Worker(service, poll_interval_s=0.01).run_once()

    engine = ReplayEngine(service)
    plan = engine.build_plan(source, overrides={"value": "y"})
    replay_id = engine.create_replay(plan)

    assert replay_id != source
    # New execution is readable and carries replay provenance in its inputs.
    assert service.status(replay_id)["status"] == "pending"
    from uap.db.repositories import ExecutionRepository

    with session_factory() as session:
        row = ExecutionRepository(session).get(uuid.UUID(replay_id))
        provenance = (row.input or {})[REPLAY_KEY]
    assert provenance["replay_of"] == source
    assert provenance["overrides"] == {"value": "y"}
