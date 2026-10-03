"""Reuse-before-delete: fork / rerun / delete one finished execution.

The user story: a user finishes a run, decides the work is worth keeping, so he
BRANCHES it (fork), REPLAYS it with new input (rerun), and only THEN deletes the
original. This module guards the three routes that make that possible:

* ``DELETE /api/executions/{id}`` — delete ONE finished run; refuse a live run
  with 409; remove artifacts (never leak silently); cascade the child rows;
  enforce the member-owns-it / admin-any rule.
* ``POST /api/executions/{id}/fork`` — branch a run; return the new execution
  id AND its correlation id; the fork must actually be RUNNABLE.
* ``POST /api/executions/{id}/rerun`` — reuse the source workflow with the
  original input (or an override).

Both id forms (durable row id and client-facing correlation id) are accepted by
every route. The tests run against PostgreSQL ``uap_test`` inside an isolated
scratch schema and skip cleanly when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.db.models.definitions import VersionStatus
from uap.db.models.execution import ExecutionStatus
from uap.db.repositories import (
    DefinitionRepository,
    EventRepository,
    ExecutionRepository,
)
from uap.runtime import CheckpointStore, ExecutionService

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"
TEST_DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL

# Reuse conftest's reachability probe so this module skips cleanly (rather than
# erroring) when PostgreSQL is down — same contract as the other DB modules.
from tests.conftest import compute_db_skip_reason  # noqa: E402

_SKIP_REASON: str | None = compute_db_skip_reason(TEST_DATABASE_URL)

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def engine(isolated_engine: Engine) -> Engine:
    """The module's isolated schema (migrations applied by ``tests/conftest.py``).

    Uses the conftest fixture (not a private schema) so ``uap.db.engine``'s
    default factory — which ``resolve_identity`` uses to look up user API keys —
    resolves to the same schema the tests write to.
    """
    return isolated_engine


@pytest.fixture()
def session_factory(isolated_session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    return isolated_session_factory


@pytest.fixture(autouse=True)
def _truncate(engine: Engine) -> Iterator[None]:
    tables = (
        "node_views",
        "decision_traces",
        "execution_checkpoints",
        "execution_events",
        "executions",
        "workflow_versions",
        "workflow_definitions",
        "users",
    )
    statement = text(
        "TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    yield
    with engine.begin() as conn:
        conn.execute(statement)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s
        s.rollback()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_execution(
    session: Session,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    *,
    correlation_id: uuid.UUID | None = None,
    requested_by: str | None = None,
    user_input: dict | None = None,
    workflow_ref: str = "research@v1",
) -> uuid.UUID:
    defs = DefinitionRepository(session)
    definition = defs.create_definition(f"wf-{uuid.uuid4().hex[:6]}")
    version = defs.create_version(
        definition.id, {"nodes": []}, status=VersionStatus.ACTIVE
    )
    payload = dict(user_input or {"value": "original input"})
    payload["__runtime__"] = {"workflow_ref": workflow_ref, "workspace_id": "default"}
    row = ExecutionRepository(session).create(
        version.id,
        status=status,
        correlation_id=correlation_id,
        requested_by=requested_by,
        input=payload,
    )
    session.flush()
    return row.id


def _child_counts(session: Session, eid: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "execution_checkpoints",
        "execution_events",
        "decision_traces",
        "node_views",
    ):
        counts[table] = int(
            session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE execution_id = :eid"),
                {"eid": eid},
            ).scalar_one()
        )
    counts["executions"] = int(
        session.execute(
            text("SELECT COUNT(*) FROM executions WHERE id = :eid"), {"eid": eid}
        ).scalar_one()
    )
    return counts


def _service(session_factory: sessionmaker[Session]) -> ExecutionService:
    def _missing(ref: str) -> None:
        raise KeyError(ref)

    return ExecutionService(
        session_factory, graph_resolver=_missing, node_runtime=None
    )


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory):
    """An app whose durable service is bound to the scratch-schema factory."""

    from uap.server import create_app

    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    return app, TestClient(app)


# --------------------------------------------------------------------------- #
# Repository / service level
# --------------------------------------------------------------------------- #


def test_repo_delete_cascades_and_reports_counts(session: Session) -> None:
    eid = _make_execution(session)
    session.commit()
    CheckpointStore(session).save(str(eid), 1, {"node_id": "a", "node_results": {}})
    EventRepository(session).append(eid, "EXECUTION_STARTED", payload={})
    session.commit()

    before = _child_counts(session, eid)
    assert before["execution_checkpoints"] >= 1
    assert before["execution_events"] >= 1

    deleted = ExecutionRepository(session).delete(eid)
    session.commit()

    assert deleted["executions"] == 1
    assert deleted["execution_checkpoints"] == before["execution_checkpoints"]
    assert deleted["execution_events"] == before["execution_events"]
    after = _child_counts(session, eid)
    assert after == {
        "execution_checkpoints": 0,
        "execution_events": 0,
        "decision_traces": 0,
        "node_views": 0,
        "executions": 0,
    }


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.PENDING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.PAUSED,
        ExecutionStatus.AWAITING_APPROVAL,
    ],
)
def test_repo_delete_refuses_live_run(session: Session, status: ExecutionStatus) -> None:
    eid = _make_execution(session, status)
    session.commit()
    with pytest.raises(ValueError):
        ExecutionRepository(session).delete(eid)
    session.rollback()
    assert ExecutionRepository(session).get(eid) is not None


def test_repo_delete_unknown_raises_lookup(session: Session) -> None:
    with pytest.raises(LookupError):
        ExecutionRepository(session).delete(uuid.uuid4())


def test_service_delete_resolves_either_id(session_factory: sessionmaker[Session]) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    svc = _service(session_factory)
    result = svc.delete_execution(str(corr))  # by CORRELATION id
    assert result["execution_id"] == str(eid)
    assert result["correlation_id"] == str(corr)
    assert result["deleted"]["executions"] == 1
    with session_factory() as s:
        assert ExecutionRepository(s).get(eid) is None


def test_service_delete_live_run_raises_value_error(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        eid = _make_execution(s, ExecutionStatus.RUNNING)
        s.commit()
    with pytest.raises(ValueError):
        _service(session_factory).delete_execution(str(eid))


def test_service_source_for_reuse_reads_input_and_meta(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        eid = _make_execution(
            s,
            requested_by="member-1",
            user_input={"value": "keep this"},
            workflow_ref="bbp@v1",
        )
        s.commit()
    src = _service(session_factory).source_for_reuse(str(eid))
    assert src["input"] == "keep this"
    assert src["workflow_ref"] == "bbp@v1"
    assert src["requested_by"] == "member-1"
    assert src["execution_id"] == str(eid)


def test_service_source_for_reuse_unknown_raises(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(LookupError):
        _service(session_factory).source_for_reuse(str(uuid.uuid4()))


def test_service_source_for_reuse_malformed_id_raises_lookup(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(LookupError):
        _service(session_factory).source_for_reuse("not-a-uuid")


def test_fork_gives_child_distinct_correlation_id(
    session_factory: sessionmaker[Session],
) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    forked = _service(session_factory).fork_execution(str(eid), label="branch")
    assert forked["execution_id"] != str(eid)
    assert forked["correlation_id"] not in (None, str(corr)), (
        "a fork must not share the source correlation id"
    )
    assert forked["status"] == "pending"


# --------------------------------------------------------------------------- #
# HTTP: DELETE
# --------------------------------------------------------------------------- #


def test_delete_requires_token_when_auth_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    from uap.server import create_app

    monkeypatch.setenv("UAP_API_TOKEN", "tok-delete")
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    with session_factory() as s:
        eid = _make_execution(s)
        s.commit()
    with TestClient(app) as client:
        assert client.delete(f"/api/executions/{eid}").status_code == 401
        ok = client.delete(
            f"/api/executions/{eid}", headers={"Authorization": "Bearer tok-delete"}
        )
        assert ok.status_code == 200


def test_delete_finished_run_by_either_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.delete(f"/api/executions/{corr}")  # correlation id
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["execution_id"] == str(eid)
        assert body["deleted"]["executions"] == 1
        assert body["artifacts"] == {"removed": [], "left_behind": []}
        # The row is gone; a second delete is 404, not a silent success.
        assert client.delete(f"/api/executions/{eid}").status_code == 404


def test_delete_live_run_is_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        eid = _make_execution(s, ExecutionStatus.RUNNING)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.delete(f"/api/executions/{eid}")
        assert resp.status_code == 409
        assert "live" in resp.json()["detail"]
    with session_factory() as s:
        assert ExecutionRepository(s).get(eid) is not None


def test_delete_removes_artifact_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    artifact_dir = tmp_path / "runs" / "artifacts" / str(corr)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("evidence", encoding="utf-8")

    with client:
        resp = client.delete(f"/api/executions/{eid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["artifacts"]["removed"] == [str(corr)]

    assert not artifact_dir.exists(), "the artifact directory leaked"


def test_delete_reports_artifact_left_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    """A directory that cannot be removed is reported, never silently ignored."""

    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    artifact_dir = tmp_path / "runs" / "artifacts" / str(corr)
    artifact_dir.mkdir(parents=True)

    import uap.server.app as app_mod

    real_rmtree = app_mod.shutil.rmtree

    def _boom(path, *args, **kwargs):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(app_mod.shutil, "rmtree", _boom)
    try:
        with client:
            resp = client.delete(f"/api/executions/{eid}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["artifacts"]["left_behind"] == [str(corr)]
    finally:
        monkeypatch.setattr(app_mod.shutil, "rmtree", real_rmtree)
        if artifact_dir.exists():
            import shutil as _shutil

            _shutil.rmtree(artifact_dir)


# --------------------------------------------------------------------------- #
# HTTP: member-owns-it / admin-any rule
# --------------------------------------------------------------------------- #


def _make_user(session: Session, role: str, name: str) -> str:
    """Create a user with a raw key; return the raw key."""

    from uap.db.repositories import UserRepository
    from uap.users import generate_api_key, hash_api_key

    raw = generate_api_key()
    UserRepository(session).create(
        name=name, role=role, api_key_hash=hash_api_key(raw)
    )
    return raw


def test_member_may_delete_only_own_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    from uap.server import create_app

    monkeypatch.setenv("UAP_API_TOKEN", "admin-tok")
    with session_factory() as s:
        member_key = _make_user(s, "member", "Member One")
        other_key = _make_user(s, "member", "Member Two")
        # Resolve the member's user id from the created user rows.
        from uap.db.repositories import UserRepository
        from uap.users import hash_api_key

        member_id = UserRepository(s).get_by_api_key_hash(
            hash_api_key(member_key)
        ).id
        other_id = UserRepository(s).get_by_api_key_hash(hash_api_key(other_key)).id
        mine = _make_execution(s, requested_by=member_id)
        theirs = _make_execution(s, requested_by=other_id)
        s.commit()

    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    with TestClient(app) as client:
        # Deleting someone else's run is forbidden and must NOT delete it.
        forbidden = client.delete(
            f"/api/executions/{theirs}",
            headers={"Authorization": f"Bearer {member_key}"},
        )
        assert forbidden.status_code == 403
        with session_factory() as s:
            assert ExecutionRepository(s).get(theirs) is not None

        # Deleting my own run succeeds.
        ok = client.delete(
            f"/api/executions/{mine}",
            headers={"Authorization": f"Bearer {member_key}"},
        )
        assert ok.status_code == 200, ok.text


def test_admin_may_delete_any_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    from uap.server import create_app

    monkeypatch.setenv("UAP_API_TOKEN", "admin-tok")
    with session_factory() as s:
        eid = _make_execution(s, requested_by="someone-else")
        s.commit()

    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/executions/{eid}",
            headers={"Authorization": "Bearer admin-tok"},
        )
        assert resp.status_code == 200, resp.text


def test_viewer_cannot_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    from uap.server import create_app

    monkeypatch.setenv("UAP_API_TOKEN", "admin-tok")
    with session_factory() as s:
        viewer_key = _make_user(s, "viewer", "Viewer")
        eid = _make_execution(s, requested_by="viewer-x")
        s.commit()

    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/executions/{eid}",
            headers={"Authorization": f"Bearer {viewer_key}"},
        )
        assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# HTTP: fork
# --------------------------------------------------------------------------- #


def test_fork_returns_new_execution_and_correlation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(s, correlation_id=corr)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.post(f"/api/executions/{eid}/fork", json={"label": "branch"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["execution_id"] != str(eid)
        assert body["correlation_id"]
        assert body["forked_from"] == str(eid)
        assert body["label"] == "branch"
        assert body["status"] == "pending"
        # The child row exists and carries a distinct correlation id.
        with session_factory() as s:
            child = ExecutionRepository(s).get(uuid.UUID(body["execution_id"]))
            assert child is not None
            assert str(child.correlation_id) == body["correlation_id"]


def test_fork_unknown_execution_is_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.post(f"/api/executions/{uuid.uuid4()}/fork", json={})
        assert resp.status_code == 404


def test_fork_requires_token_when_auth_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    from uap.server import create_app

    monkeypatch.setenv("UAP_API_TOKEN", "tok-fork")
    with session_factory() as s:
        eid = _make_execution(s)
        s.commit()
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    app.state.service = _service(session_factory)
    with TestClient(app) as client:
        assert client.post(f"/api/executions/{eid}/fork", json={}).status_code == 401


def test_fork_without_slice_says_caller_must_run_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    """No durable slice wired -> the response must say the fork was not started."""

    with session_factory() as s:
        eid = _make_execution(s)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    app.state.slice = None
    with client:
        resp = client.post(f"/api/executions/{eid}/fork", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["started"] is False
        assert "next_step" in body and body["next_step"]


# --------------------------------------------------------------------------- #
# HTTP: rerun
# --------------------------------------------------------------------------- #


def test_rerun_reuses_original_input_when_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        eid = _make_execution(s, user_input={"value": "the original goal"})
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.post(f"/api/executions/{eid}/rerun", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rerun_of"] == str(eid)
        assert body["reused_original_input"] is True
        assert body["input"] == "the original goal"


def test_rerun_with_override_runs_new_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        eid = _make_execution(s, user_input={"value": "old goal"})
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.post(
            f"/api/executions/{eid}/rerun", json={"input": "a brand new goal"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reused_original_input"] is False
        assert body["input"] == "a brand new goal"


def test_rerun_unknown_execution_is_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        resp = client.post(f"/api/executions/{uuid.uuid4()}/rerun", json={})
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The full story: reuse then delete, proving nothing leaks
# --------------------------------------------------------------------------- #


def test_reuse_then_delete_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    corr = uuid.uuid4()
    with session_factory() as s:
        eid = _make_execution(
            s, correlation_id=corr, user_input={"value": "reuse me"}
        )
        # Seed child rows so the cascade has something to remove.
        CheckpointStore(s).save(str(eid), 1, {"node_id": "a", "node_results": {}})
        EventRepository(s).append(eid, "EXECUTION_STARTED", payload={})
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    artifact_dir = tmp_path / "runs" / "artifacts" / str(corr)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "a.md").write_text("x", encoding="utf-8")

    with client:
        with session_factory() as s:
            initial = _child_counts(s, eid)
        assert initial["executions"] == 1
        assert initial["execution_events"] >= 1
        assert initial["execution_checkpoints"] >= 1

        # 1. reuse the work: fork + rerun
        assert client.post(f"/api/executions/{corr}/fork", json={}).status_code == 200
        assert client.post(
            f"/api/executions/{corr}/rerun", json={"input": "changed"}
        ).status_code == 200

        # 2. delete the original. Snapshot AFTER reuse: forking records an
        #    EXECUTION_FORKED event on the source, so the source's own counts
        #    grow between the initial read and the delete.
        with session_factory() as s:
            before = _child_counts(s, eid)
        resp = client.delete(f"/api/executions/{corr}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"]["executions"] == 1
        assert body["deleted"]["execution_events"] == before["execution_events"]
        assert (
            body["deleted"]["execution_checkpoints"]
            == before["execution_checkpoints"]
        )
        assert body["artifacts"]["removed"] == [str(corr)]
        assert body["artifacts"]["left_behind"] == []

    # 3. prove nothing leaked
    with session_factory() as s:
        after = _child_counts(s, eid)
    assert after["executions"] == 0
    assert after["execution_events"] == 0
    assert after["execution_checkpoints"] == 0
    assert after["decision_traces"] == 0
    assert after["node_views"] == 0
    assert not artifact_dir.exists()


# --------------------------------------------------------------------------- #
# End-to-end: the fork really RUNS (real PlatformSlice, real durable rows)
# --------------------------------------------------------------------------- #


def test_fork_of_a_real_run_actually_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    """A fork of a real slice run must reach a terminal status, not sit pending.

    This is the "do not hand back a dead id" guarantee exercised against the
    REAL stack: create a run through ``POST /tasks``, fork it, and assert the
    fork's durable status advances past ``pending`` (the route drives it through
    the same slice/worker path as ``/resume``).
    """

    from uap.server import create_app

    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    client = TestClient(app)

    with client:
        created = client.post(
            "/tasks",
            json={"input": "research the best langgraph checkpointer approach"},
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]

        fork_resp = client.post(
            f"/api/executions/{task_id}/fork", json={"label": "e2e"}
        )
        assert fork_resp.status_code == 200, fork_resp.text
        body = fork_resp.json()
        assert body["started"] is True
        fork_id = body["execution_id"]

        # Read the fork's DURABLE status straight from PostgreSQL.
        from uap.db.repositories import ExecutionRepository

        with session_factory() as s:
            row = ExecutionRepository(s).get(uuid.UUID(fork_id))
            assert row is not None, "the fork row was never created"
            assert row.status.value in ("completed", "failed"), (
                f"fork did not run: status={row.status.value!r}"
            )
            # Distinct identity from the source, on both id forms.
            assert str(row.id) != body["forked_from"]
            assert row.correlation_id is not None
            assert str(row.correlation_id) == body["correlation_id"]
