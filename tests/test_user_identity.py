"""Tests for user identity and requester attribution (auditing "who ran what").

Attribution, not authentication:
A caller can claim any user_id, so requested_by is an audit trail of who
requested the run, not a security boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory, get_database_url
from uap.db.repositories import DefinitionRepository, ExecutionRepository
from uap.runtime.service import ExecutionService
from uap.server.app import create_app


RESEARCH_INPUT = "research database indexing strategies"
TEST_DATABASE_URL = get_database_url()


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A dedicated scratch schema so parallel agents on DB never race."""
    from sqlalchemy.engine import make_url

    schema = f"user_ident_{uuid.uuid4().hex[:8]}"
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


def test_execution_model_and_repo_persistence(session_factory) -> None:
    """ExecutionRepository.create persists requested_by, and None stays NULL (not bogus default)."""
    with session_factory() as session:
        d_repo = DefinitionRepository(session)
        defn = d_repo.create_definition(f"wf-{uuid.uuid4().hex[:8]}")
        ver = d_repo.create_version(defn.id, {"nodes": ["a"]})

        repo = ExecutionRepository(session)

        # 1. Attributed run
        row_alice = repo.create(
            ver.id,
            input={"test": 1},
            requested_by="alice",
        )
        assert getattr(row_alice, "requested_by", None) == "alice"

        # 2. Anonymous / omitted requester MUST be NULL in DB, not a bogus default
        row_anon = repo.create(
            ver.id,
            input={"test": 2},
            requested_by=None,
        )
        assert getattr(row_anon, "requested_by", None) is None

        # Flush and reload from DB to verify real column persistence
        session.flush()
        session.expire_all()

        loaded_alice = repo.get(row_alice.id)
        assert loaded_alice is not None
        assert getattr(loaded_alice, "requested_by", None) == "alice"

        loaded_anon = repo.get(row_anon.id)
        assert loaded_anon is not None
        assert getattr(loaded_anon, "requested_by", None) is None

        # Filter by requested_by in repo.list
        alice_list = repo.list(requested_by="alice")
        assert any(r.id == row_alice.id for r in alice_list)
        assert not any(r.id == row_anon.id for r in alice_list)


def test_execution_service_status_projection_includes_requested_by(session_factory) -> None:
    """ExecutionService.enqueue persists requested_by, and status projection exposes it."""
    from uap.graph import WorkflowGraph

    wf_name = f"wf-{uuid.uuid4().hex[:8]}"

    def resolver(ref: str):
        return WorkflowGraph(id=wf_name, name=wf_name, nodes=[], edges=[])

    svc = ExecutionService(session_factory, graph_resolver=resolver, node_runtime=object())

    with session_factory() as session:
        d_repo = DefinitionRepository(session)
        defn = d_repo.create_definition(wf_name)
        d_repo.create_version(defn.id, {"nodes": []})
        session.commit()

    eid_alice = svc.enqueue(
        workflow_ref=wf_name,
        inputs={"x": 1},
        requested_by="alice",
    )
    status_alice = svc.status(eid_alice)
    assert status_alice.get("requested_by") == "alice"

    eid_anon = svc.enqueue(
        workflow_ref=wf_name,
        inputs={"x": 2},
    )
    status_anon = svc.status(eid_anon)
    assert status_anon.get("requested_by") is None


def test_api_tasks_user_identity_flow_and_filtering(tmp_path: Path) -> None:
    """POST /tasks sets requested_by, GET /tasks and GET /tasks/{id} expose it, and ?requested_by filters it."""
    app = create_app(runs_dir=tmp_path, run_inline=True)
    client = TestClient(app)

    # 1. Run as alice
    res_alice = client.post("/tasks", json={"input": RESEARCH_INPUT, "user_id": "alice"})
    assert res_alice.status_code == 200, res_alice.text
    alice_id = res_alice.json()["task_id"]

    # 2. Run as bob
    res_bob = client.post("/tasks", json={"input": RESEARCH_INPUT, "user_id": "bob"})
    assert res_bob.status_code == 200, res_bob.text
    bob_id = res_bob.json()["task_id"]

    # 3. Run with omitted user_id -> MUST yield NULL, not 'web-user'
    res_anon = client.post("/tasks", json={"input": RESEARCH_INPUT})
    assert res_anon.status_code == 200, res_anon.text
    anon_id = res_anon.json()["task_id"]

    # Detail GET /tasks/{id} includes requested_by
    detail_alice = client.get(f"/tasks/{alice_id}").json()
    assert detail_alice.get("requested_by") == "alice"

    detail_bob = client.get(f"/tasks/{bob_id}").json()
    assert detail_bob.get("requested_by") == "bob"

    detail_anon = client.get(f"/tasks/{anon_id}").json()
    assert detail_anon.get("requested_by") is None

    # Summary GET /tasks includes requested_by
    all_tasks = client.get("/tasks").json()
    task_map = {t["task_id"]: t for t in all_tasks}
    assert task_map[alice_id].get("requested_by") == "alice"
    assert task_map[bob_id].get("requested_by") == "bob"
    assert task_map[anon_id].get("requested_by") is None

    # Filtering by ?requested_by=alice
    alice_only = client.get("/tasks?requested_by=alice").json()
    alice_ids = {t["task_id"] for t in alice_only}
    assert alice_id in alice_ids
    assert bob_id not in alice_ids
    assert anon_id not in alice_ids

    # Filtering by ?requested_by=bob
    bob_only = client.get("/tasks?requested_by=bob").json()
    bob_ids = {t["task_id"] for t in bob_only}
    assert bob_id in bob_ids
    assert alice_id not in bob_ids
    assert anon_id not in bob_ids

    # Filtering by nonexistent user -> empty list
    charlie_only = client.get("/tasks?requested_by=charlie").json()
    assert charlie_only == []
