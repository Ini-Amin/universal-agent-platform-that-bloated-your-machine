"""Retention/pruning tests: opt-in deletion of old, terminal executions.

The guarantee under test: unless an operator explicitly calls ``prune`` (repo,
service, HTTP endpoint, or CLI), NOTHING is ever deleted — there is no
background timer and no default retention. When prune *is* called it must:

* delete executions older than the cutoff whose status is terminal
  (``completed`` / ``failed`` / ``cancelled``);
* remove that execution's children (checkpoints, events, decision traces) via
  the ``ON DELETE CASCADE`` foreign keys;
* never touch a recent terminal run, or a live run (``pending`` / ``running``),
  however old it is;
* honour the optional per-call ``limit``.

These tests run against the shared PostgreSQL ``uap_test`` database inside an
isolated scratch schema (same pattern as ``test_durable_runtime.py``); they
skip cleanly when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory
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

_SKIP_REASON: str | None = None
try:
    _engine = create_db_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 2})
    with _engine.connect() as _conn:
        _conn.execute(text("SELECT 1"))
    _engine.dispose()
except Exception as exc:  # noqa: BLE001
    _SKIP_REASON = f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")

CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
OLD = CUTOFF - timedelta(days=30)
RECENT = CUTOFF + timedelta(days=1)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Isolated scratch schema, mirroring test_durable_runtime.py."""

    from sqlalchemy.engine import make_url

    schema = f"retention_{uuid.uuid4().hex[:8]}"
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
    tables = ("executions", "workflow_versions", "workflow_definitions")
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
    session: Session, status: ExecutionStatus, created_at: datetime
) -> uuid.UUID:
    defs = DefinitionRepository(session)
    definition = defs.create_definition(f"wf-{uuid.uuid4().hex[:6]}")
    version = defs.create_version(
        definition.id, {"nodes": []}, status=VersionStatus.ACTIVE
    )
    row = ExecutionRepository(session).create(version.id, status=status)
    # created_at is a server default; override it to simulate age.
    session.execute(
        text("UPDATE executions SET created_at = :ts WHERE id = :eid"),
        {"ts": created_at, "eid": row.id},
    )
    session.flush()
    return row.id


def _child_counts(session: Session, eid: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("execution_checkpoints", "execution_events", "decision_traces"):
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

# --------------------------------------------------------------------------- #
# Repository-level behaviour
# --------------------------------------------------------------------------- #

def test_prune_deletes_old_terminal_run_and_cascades_children(
    session: Session,
) -> None:
    eid = _make_execution(session, ExecutionStatus.COMPLETED, OLD)
    session.commit()

    CheckpointStore(session).save(str(eid), 1, {"node": "a", "state": {"v": 1}})
    EventRepository(session).append(eid, "EXECUTION_STARTED", payload={})
    session.commit()

    before = _child_counts(session, eid)
    assert before["execution_checkpoints"] >= 1
    assert before["execution_events"] >= 1

    deleted = ExecutionRepository(session).prune(CUTOFF)
    session.commit()

    assert deleted == 1
    after = _child_counts(session, eid)
    assert after["executions"] == 0
    assert after["execution_checkpoints"] == 0
    assert after["execution_events"] == 0
    assert after["decision_traces"] == 0


def test_prune_keeps_recent_terminal_and_old_live_runs(session: Session) -> None:
    old_completed = _make_execution(session, ExecutionStatus.COMPLETED, OLD)
    old_failed = _make_execution(session, ExecutionStatus.FAILED, OLD)
    old_cancelled = _make_execution(session, ExecutionStatus.CANCELLED, OLD)
    recent_completed = _make_execution(session, ExecutionStatus.COMPLETED, RECENT)
    old_running = _make_execution(session, ExecutionStatus.RUNNING, OLD)
    old_pending = _make_execution(session, ExecutionStatus.PENDING, OLD)
    session.commit()

    deleted = ExecutionRepository(session).prune(CUTOFF)
    session.commit()

    assert deleted == 3
    repo = ExecutionRepository(session)
    assert repo.get(old_completed) is None
    assert repo.get(old_failed) is None
    assert repo.get(old_cancelled) is None
    # A recent terminal run and both live runs survive.
    assert repo.get(recent_completed) is not None
    assert repo.get(old_running) is not None
    assert repo.get(old_pending) is not None


def test_prune_limit_caps_rows_deleted_per_call(session: Session) -> None:
    for _ in range(5):
        _make_execution(session, ExecutionStatus.COMPLETED, OLD)
    session.commit()

    repo = ExecutionRepository(session)
    assert repo.prune(CUTOFF, limit=2) == 2
    session.commit()
    remaining = repo.prune(CUTOFF)
    session.commit()
    assert remaining == 3


def test_prune_with_nothing_old_deletes_nothing(session: Session) -> None:
    eid = _make_execution(session, ExecutionStatus.COMPLETED, RECENT)
    session.commit()
    assert ExecutionRepository(session).prune(CUTOFF) == 0
    session.commit()
    assert ExecutionRepository(session).get(eid) is not None

# --------------------------------------------------------------------------- #
# Service-level behaviour
# --------------------------------------------------------------------------- #

def test_service_prune_executions_delegates(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        old_id = _make_execution(s, ExecutionStatus.COMPLETED, OLD)
        recent_id = _make_execution(s, ExecutionStatus.COMPLETED, RECENT)
        s.commit()

    deleted = _service(session_factory).prune_executions(CUTOFF)
    assert deleted == 1

    with session_factory() as s:
        repo = ExecutionRepository(s)
        assert repo.get(old_id) is None
        assert repo.get(recent_id) is not None


def test_service_prune_never_deletes_live_runs(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        eid = _make_execution(s, ExecutionStatus.RUNNING, OLD)
        s.commit()
    assert _service(session_factory).prune_executions(CUTOFF) == 0
    with session_factory() as s:
        assert ExecutionRepository(s).get(eid) is not None

# --------------------------------------------------------------------------- #
# HTTP endpoint behaviour
# --------------------------------------------------------------------------- #

TOKEN = "retention-token"


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory):
    """An app whose durable service is bound to the scratch-schema factory."""

    from fastapi.testclient import TestClient

    from uap.server import create_app

    app = create_app(
        runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05
    )
    app.state.service = _service(session_factory)
    return app, TestClient(app)


def test_endpoint_requires_token_when_auth_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        no_token = client.post(
            "/api/maintenance/prune", json={"older_than_days": 1}
        )
        assert no_token.status_code == 401
        with_token = client.post(
            "/api/maintenance/prune",
            json={"older_than_days": 1},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert with_token.status_code == 200


def test_endpoint_prunes_old_terminal_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    with session_factory() as s:
        old_id = _make_execution(s, ExecutionStatus.COMPLETED, OLD)
        recent_id = _make_execution(s, ExecutionStatus.COMPLETED, RECENT)
        s.commit()

    app, client = _client(monkeypatch, tmp_path, session_factory)
    days = (datetime.now(timezone.utc) - OLD).days
    with client:
        resp = client.post("/api/maintenance/prune", json={"older_than_days": days})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"deleted": 1, "older_than_days": days}

    with session_factory() as s:
        repo = ExecutionRepository(s)
        assert repo.get(old_id) is None
        assert repo.get(recent_id) is not None


def test_endpoint_validates_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        assert client.post("/api/maintenance/prune", json={}).status_code == 422
        assert (
            client.post(
                "/api/maintenance/prune",
                json={"older_than_days": 1, "limit": 0},
            ).status_code
            == 422
        )

# --------------------------------------------------------------------------- #
# Opt-in default: doing nothing deletes nothing
# --------------------------------------------------------------------------- #

def test_no_prune_without_explicit_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session_factory: sessionmaker[Session]
) -> None:
    """Booting the app and importing the CLI must not delete anything."""

    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    with session_factory() as s:
        old_id = _make_execution(s, ExecutionStatus.COMPLETED, OLD)
        old_running = _make_execution(s, ExecutionStatus.RUNNING, OLD)
        s.commit()

    import uap.maintenance  # noqa: F401  (import alone must be inert)

    app, client = _client(monkeypatch, tmp_path, session_factory)
    with client:
        # A read-only route and the health of the app: no prune is triggered.
        assert client.get("/api/workflows").status_code == 200

    with session_factory() as s:
        repo = ExecutionRepository(s)
        assert repo.get(old_id) is not None
        assert repo.get(old_running) is not None


# --------------------------------------------------------------------------- #
# CLI footgun guard: --older-than-days 0 means "cutoff = now", i.e. every
# terminal run is eligible, including one created a second ago. Verified live
# on 2026-10-03: an unguarded `--older-than-days 0` deleted all 2388 rows.
# --------------------------------------------------------------------------- #


def test_cli_refuses_zero_days_without_confirmation(capsys) -> None:
    from uap.maintenance import main

    code = main(["prune", "--older-than-days", "0"])

    assert code == 2, "zero-day prune must not proceed without --yes"
    err = capsys.readouterr().err
    assert "refusing" in err.lower()
    assert "--yes" in err


def test_cli_allows_zero_days_with_confirmation(monkeypatch) -> None:
    """--yes is the explicit second signal; it must reach prune()."""
    import uap.maintenance as maintenance

    calls = {}

    def fake_prune(older_than_days, *, limit=None):
        calls["days"] = older_than_days
        calls["limit"] = limit
        return 0

    monkeypatch.setattr(maintenance, "prune", fake_prune)
    code = maintenance.main(["prune", "--older-than-days", "0", "--yes"])

    assert code == 0
    assert calls["days"] == 0


def test_cli_normal_age_needs_no_confirmation(monkeypatch) -> None:
    """A real age must not require --yes; the guard is only for the footgun."""
    import uap.maintenance as maintenance

    calls = {}
    monkeypatch.setattr(
        maintenance, "prune", lambda d, *, limit=None: calls.setdefault("days", d) or 0
    )
    code = maintenance.main(["prune", "--older-than-days", "30"])

    assert code == 0
    assert calls["days"] == 30
