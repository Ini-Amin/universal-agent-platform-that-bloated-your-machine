"""Tests for truthful, meaningful cooperative pause and resume."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory
from uap.db.engine import session_scope
from uap.db.models.execution import Execution, ExecutionStatus
from uap.db.repositories import DefinitionRepository, ExecutionRepository
from uap.graph import EdgeKind, GraphEdge, GraphNode, NodeKind, Port, PortType, WorkflowGraph
from uap.runtime import CheckpointStore, ExecutionService, Worker
from uap.server import create_app


# Dedicated scratch schema for test isolation
_TEST_SCHEMA = f"pause_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    raw = create_db_engine()
    with raw.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_TEST_SCHEMA}"'))
        conn.commit()

    schema_engine = create_db_engine(
        connect_args={"options": f"-csearch_path={_TEST_SCHEMA},public"}
    )
    # Base already imported at module level

    Base.metadata.create_all(schema_engine)
    try:
        yield schema_engine
    finally:
        schema_engine.dispose()
        with raw.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_TEST_SCHEMA}" CASCADE'))
            conn.commit()
        raw.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
def _truncate(engine: Engine) -> Iterator[None]:
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


def _linear_graph(name: str = "linear_p") -> WorkflowGraph:
    port = Port(name="value", type=PortType.ANY, required=False)
    in_node = GraphNode(id="input", kind=NodeKind.INPUT, outputs=[port])
    node_a = GraphNode(id="node_a", kind=NodeKind.AGENT, inputs=[port], outputs=[port])
    node_b = GraphNode(id="node_b", kind=NodeKind.AGENT, inputs=[port], outputs=[port])
    out_node = GraphNode(id="output", kind=NodeKind.OUTPUT, inputs=[port], outputs=[port])
    edges = [
        GraphEdge(id="e1", source="input", source_port="value", target="node_a", target_port="value", kind=EdgeKind.DATA),
        GraphEdge(id="e2", source="node_a", source_port="value", target="node_b", target_port="value", kind=EdgeKind.DATA),
        GraphEdge(id="e3", source="node_b", source_port="value", target="output", target_port="value", kind=EdgeKind.DATA),
    ]
    return WorkflowGraph(id=name, nodes=[in_node, node_a, node_b, out_node], edges=edges, name=name)


class _TrackingRuntime:
    def __init__(self, pause_callback: Any | None = None) -> None:
        self.pause_callback = pause_callback
        self.executed_nodes: list[str] = []

    async def run_node(self, node: Any, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.executed_nodes.append(node.id)
        if node.id == "node_a" and self.pause_callback is not None:
            self.pause_callback(str(getattr(ctx, "execution_id", "")))
        return {"value": f"{node.id}_done"}

def _seed_workflow(session_factory: Any, name: str) -> None:
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        defn = repo.create_definition(name)
        repo.create_version(defn.id, spec={"nodes": ["input", "node_a", "node_b", "output"]})

@pytest.mark.asyncio
async def test_service_pause_mid_flight_and_resume_from_checkpoint(session_factory):
    """A running task is paused mid-flight, stays paused truthfully, and resumes without re-running earlier nodes."""
    _seed_workflow(session_factory, "linear_p")
    def do_pause(eid: str) -> None:
        service.pause_request(eid)

    runtime = _TrackingRuntime(pause_callback=do_pause)
    service = ExecutionService(
        session_factory,
        graph_resolver=lambda ref: _linear_graph(),
        node_runtime=runtime,
    )

    execution_id = service.enqueue(workflow_ref="linear_p@v1", inputs={"value": "test"})
    assert service.status(execution_id)["status"] == "pending"

    worker = Worker(service, worker_id="test_worker")
    # Worker runs node_a, where pause_request is triggered, pausing before node_b
    await worker.run_specific(execution_id)

    # Status must be truthfully 'paused', NOT 'completed'
    status = service.status(execution_id)
    assert status["status"] == "paused", f"Expected paused, got {status['status']}"

    # Checkpoint store must have saved progress for the completed node
    checkpoints = service.checkpoints(execution_id)
    assert len(checkpoints) >= 1
    _seq, last_state = checkpoints[-1]
    completed_nodes = [nid for nid, st in last_state.get("node_status", {}).items() if st == "completed"]
    assert "node_a" in completed_nodes
    assert "node_b" not in completed_nodes  # node_b did NOT run yet

    nodes_before_resume = list(runtime.executed_nodes)
    assert "node_b" not in nodes_before_resume

    # Resume: status becomes pending (re-queued)
    service.resume(execution_id)
    assert service.status(execution_id)["status"] == "pending"

    # Worker drives it again from checkpoint
    await worker.run_specific(execution_id)
    final_status = service.status(execution_id)
    assert final_status["status"] == "completed"
    assert final_status["resume_count"] == 1

    # node_a was NOT executed a second time on resume!
    assert runtime.executed_nodes.count("node_a") == 1
    assert runtime.executed_nodes.count("node_b") == 1


@pytest.mark.asyncio
async def test_api_pause_stays_paused_then_resumes(tmp_path: Path):
    """Test full HTTP API pause and resume behavior: mid-flight pause stays paused, then resumes to completed."""
    from httpx import ASGITransport, AsyncClient

    app = create_app(runs_dir=tmp_path / "runs", run_inline=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Start a research task
        resp = await client.post("/tasks", json={"input": "research distributed cache coherence protocols"})
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        # Pause immediately
        pause_res = await client.post(f"/api/executions/{task_id}/pause")
        assert pause_res.status_code == 200
        assert pause_res.json()["status"] == "paused"

        # Assert that task status is 'paused' and STAYS 'paused'
        await asyncio.sleep(0.1)
        st1 = (await client.get(f"/tasks/{task_id}")).json()["status"]
        assert st1 == "paused"

        await asyncio.sleep(0.2)
        st2 = (await client.get(f"/tasks/{task_id}")).json()["status"]
        assert st2 == "paused"

        # Resume the execution
        resume_res = await client.post(f"/api/executions/{task_id}/resume")
        assert resume_res.status_code == 200
        assert resume_res.json()["status"] == "running"

        # Wait for completion
        completed = False
        for _ in range(40):
            await asyncio.sleep(0.1)
            cur = (await client.get(f"/tasks/{task_id}")).json()["status"]
            if cur == "completed":
                completed = True
                break
            assert cur in ("running", "accepted"), f"Unexpected intermediate status {cur}"

        assert completed, "Resumed task did not reach completed status"
