"""Tests for the Workspace model, detection and store (Master section 3).

Detection is deterministic and database-free, so those tests always run. Store
tests need the ``uap.db`` package (built by another agent in parallel); they
skip cleanly with a clear reason until it lands, and exercise the real code path
against ``uap_test`` once it does. The store's delegation logic is additionally
covered with an injected in-memory repository so it is exercised either way.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from uap.contracts.models import Domain, TaskSpec
from uap.workspace import (
    DEFAULT_THRESHOLD,
    Candidate,
    DetectionResult,
    Workspace,
    WorkspaceDetector,
    WorkspaceStatus,
    WorkspaceStore,
)

# --------------------------------------------------------------------------- #
# Database plumbing (skipped when uap.db is not built yet)
# --------------------------------------------------------------------------- #

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _test_database_url() -> str:
    """Test database URL: explicit env override, else the local ``uap_test``.

    ``DATABASE_URL`` is the same convention ``uap.db.engine`` reads (Master
    section 62); when it is unset we point at the dedicated test database.
    """
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("UAP_DATABASE_URL")
        or DEFAULT_TEST_URL
    )


from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _TestBase(DeclarativeBase):
    """Throwaway metadata used only when uap.db has no Workspace table yet."""


class _TestWorkspace(_TestBase):
    __tablename__ = "test_workspaces_phase1"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    root_path: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    default_workflow_refs: Mapped[list] = mapped_column(JSON, default=list)


def _workspace_orm_model():
    """Return a Workspace ORM model: the real uap.db one, else the test table.

    The database package is built in parallel; its first cut models definitions
    and executions. When it ships a ``Workspace`` table we use it directly;
    otherwise the store is still exercised end-to-end against a throwaway table
    created on ``uap_test`` (the same session/URL convention).
    """
    try:
        from uap.workspace.store import _load_workspace_model

        return _load_workspace_model()
    except ImportError:
        return _TestWorkspace


def _database_reachable() -> bool:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        return False
    try:
        engine = create_engine(_test_database_url(), future=True)
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - any connection failure means "skip"
        return False


requires_db = pytest.mark.skipif(
    not _database_reachable(),
    reason=(
        "no reachable PostgreSQL test database; workspace store tests skip "
        "(set DATABASE_URL or start the local uap_test database)"
    ),
)


@pytest.fixture()
def db_session():
    """A real SQLAlchemy session against ``uap_test`` with a workspaces table."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    model = _workspace_orm_model()
    engine = create_engine(_test_database_url(), future=True)
    model.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    # The store commits, so rows survive between runs; start from a clean table.
    session.query(model).delete()
    session.commit()
    try:
        yield session
    finally:
        session.query(model).delete()
        session.commit()
        session.close()
        engine.dispose()


def _workspace(ws_id: str, name: str, description: str = "", tags=None) -> Workspace:
    return Workspace(
        id=ws_id,
        name=name,
        description=description,
        settings={"tags": list(tags or [])},
    )


def _task(goal: str, **input_data) -> TaskSpec:
    return TaskSpec(domain=Domain.BBP, goal=goal, input=dict(input_data))


# --------------------------------------------------------------------------- #
# 1. create / get round-trip (db)
# --------------------------------------------------------------------------- #

@requires_db
def test_store_create_get_round_trip(db_session) -> None:
    store = WorkspaceStore(db_session, model=_workspace_orm_model())
    original = _workspace("ws-1", "Security Research / Target X", "bbp recon", ["recon"])
    store.create(original)

    loaded = store.get("ws-1")
    assert loaded is not None
    assert loaded.id == "ws-1"
    assert loaded.name == "Security Research / Target X"
    assert loaded.description == "bbp recon"
    assert loaded.status == WorkspaceStatus.ACTIVE
    assert loaded.settings["tags"] == ["recon"]
    assert loaded.created_at.tzinfo is not None

    assert store.get("does-not-exist") is None


# --------------------------------------------------------------------------- #
# 2. list / update / delete (db)
# --------------------------------------------------------------------------- #

@requires_db
def test_store_list_update_delete(db_session) -> None:
    store = WorkspaceStore(db_session, model=_workspace_orm_model())
    store.create(_workspace("ws-1", "Alpha"))
    store.create(_workspace("ws-2", "Beta"))

    listed = store.list()
    assert {w.id for w in listed} == {"ws-1", "ws-2"}

    updated = store.update(
        _workspace("ws-1", "Alpha Renamed", "now with a description")
    )
    assert updated.name == "Alpha Renamed"
    assert store.get("ws-1").description == "now with a description"

    assert store.delete("ws-2") is True
    assert store.get("ws-2").status == WorkspaceStatus.DELETED
    assert {w.id for w in store.list()} == {"ws-1"}
    assert "ws-2" in {w.id for w in store.list(include_deleted=True)}
    assert store.delete("ws-2") is True  # idempotent soft delete


# --------------------------------------------------------------------------- #
# Store delegation with an injected repository (always runs)
# --------------------------------------------------------------------------- #

class _FakeWorkspaceRepository:
    """In-memory stand-in implementing the pinned WorkspaceStore interface."""

    def __init__(self) -> None:
        self._rows: dict[str, Workspace] = {}

    def create(self, workspace: Workspace) -> Workspace:
        self._rows[workspace.id] = workspace
        return workspace

    def get(self, workspace_id: str) -> Workspace | None:
        return self._rows.get(workspace_id)

    def list(self) -> list[Workspace]:
        return list(self._rows.values())

    def update(self, workspace: Workspace) -> Workspace:
        if workspace.id not in self._rows:
            raise KeyError(workspace.id)
        self._rows[workspace.id] = workspace
        return workspace

    def delete(self, workspace_id: str) -> bool:
        row = self._rows.get(workspace_id)
        if row is None:
            return False
        self._rows[workspace_id] = row.model_copy(
            update={"status": WorkspaceStatus.DELETED}
        )
        return True


def test_store_delegates_to_injected_repository() -> None:
    store = WorkspaceStore(session=None, repository=_FakeWorkspaceRepository())
    store.create(_workspace("ws-1", "Alpha"))
    assert store.get("ws-1").name == "Alpha"
    assert [w.id for w in store.list()] == ["ws-1"]
    assert store.delete("ws-1") is True
    assert store.get("ws-1").status == WorkspaceStatus.DELETED


# --------------------------------------------------------------------------- #
# 3. Detection: explicit hint wins
# --------------------------------------------------------------------------- #

def test_detection_exact_hint_wins_by_id() -> None:
    ws_high = _workspace("ws-alpha", "Security Recon", "bug bounty recon", ["recon"])
    ws_hinted = _workspace("ws-beta", "Literature Review", "academic papers")
    # The task vocabulary clearly favours ws-alpha, but the hint must override.
    task = _task("security recon bug bounty", workspace="ws-beta")

    result = WorkspaceDetector().detect(task, [ws_high, ws_hinted])

    assert isinstance(result, DetectionResult)
    assert result.best is not None
    assert result.best.id == "ws-beta"
    assert result.confidence == 1.0
    assert "id" in result.reason


def test_detection_exact_hint_wins_by_name() -> None:
    ws_high = _workspace("ws-alpha", "Security Recon", "bug bounty recon", ["recon"])
    ws_hinted = _workspace("ws-beta", "Literature Review", "academic papers")
    task = _task("security recon bug bounty", workspace="Literature Review")

    result = WorkspaceDetector().detect(task, [ws_high, ws_hinted])
    assert result.best is not None and result.best.id == "ws-beta"
    assert result.confidence == 1.0


def test_detection_unresolvable_hint_is_reported_not_ignored() -> None:
    ws = _workspace("ws-alpha", "Security Recon", "bug bounty recon")
    task = _task("security recon", workspace="ghost-workspace")

    result = WorkspaceDetector().detect(task, [ws])
    assert result.best is None
    assert "did not match" in result.reason
    # It never silently falls back to a scored guess.
    assert result.confidence == 0.0


# --------------------------------------------------------------------------- #
# 4. Detection: keyword match ranks correctly
# --------------------------------------------------------------------------- #

def test_detection_keyword_match_ranks_clear_winner() -> None:
    ws_security = _workspace(
        "ws-sec", "Security Research Target X", "bug bounty recon for target x",
        ["security", "recon"],
    )
    ws_literature = _workspace(
        "ws-lit", "Literature Review", "academic paper survey"
    )
    task = _task("security recon bug bounty target x")

    result = WorkspaceDetector().detect(task, [ws_literature, ws_security])

    assert result.best is not None
    assert result.best.id == "ws-sec"
    assert result.confidence > DEFAULT_THRESHOLD
    assert result.candidates[0].workspace.id == "ws-sec"
    assert result.candidates[0].score > result.candidates[1].score


def test_detection_is_order_independent() -> None:
    ws_a = _workspace("ws-a", "Security Recon", "bug bounty target recon")
    ws_b = _workspace("ws-b", "Literature Review", "academic papers")
    task = _task("security recon bug bounty target")

    first = WorkspaceDetector().detect(task, [ws_a, ws_b])
    second = WorkspaceDetector().detect(task, [ws_b, ws_a])
    assert first.best.id == second.best.id == "ws-a"
    assert first.confidence == second.confidence


# --------------------------------------------------------------------------- #
# 5. Detection: ambiguous tie -> best=None, reason "ambiguous"
# --------------------------------------------------------------------------- #

def test_detection_ambiguous_tie_returns_none() -> None:
    ws_one = _workspace("ws-1", "Alpha Target Security", "security target")
    ws_two = _workspace("ws-2", "Beta Target Security", "security target")
    task = _task("security target")

    result = WorkspaceDetector().detect(task, [ws_one, ws_two])

    assert result.best is None
    assert result.reason == "ambiguous"
    assert result.ambiguous is True
    assert {c.workspace.id for c in result.candidates} >= {"ws-1", "ws-2"}
    assert result.confidence >= DEFAULT_THRESHOLD


# --------------------------------------------------------------------------- #
# 6. Detection: no match -> best=None with reason
# --------------------------------------------------------------------------- #

def test_detection_no_match_returns_none_with_reason() -> None:
    ws = _workspace("ws-1", "Literature Review", "academic papers")
    task = _task("deploy kubernetes cluster")

    result = WorkspaceDetector().detect(task, [ws])

    assert result.best is None
    assert result.reason != "ambiguous"
    assert "below threshold" in result.reason
    assert result.confidence < DEFAULT_THRESHOLD


def test_detection_empty_library_returns_none() -> None:
    result = WorkspaceDetector().detect(_task("anything"), [])
    assert result.best is None
    assert result.candidates == ()
    assert result.confidence == 0.0
    assert "no workspaces" in result.reason


# --------------------------------------------------------------------------- #
# 7. Detection: threshold respected
# --------------------------------------------------------------------------- #

def test_detection_threshold_respected() -> None:
    # coverage = 2/3 -> score = 0.6 * 0.6667 = 0.4, below the default 0.5. The
    # workspace name ("Project X") is not in the task text, so the name bonus is
    # 0 and coverage is the whole score; the tags supply the two matched terms.
    ws = _workspace("ws-1", "Project X", tags=["alpha", "beta"])
    task = _task("alpha beta gamma")

    strict = WorkspaceDetector().detect(task, [ws])
    assert strict.best is None
    assert strict.confidence == pytest.approx(0.4, abs=1e-9)
    assert "below threshold" in strict.reason

    # The same score clears a lower threshold -- proving the gate, not the score.
    lenient = WorkspaceDetector(threshold=0.3).detect(task, [ws])
    assert lenient.best is not None and lenient.best.id == "ws-1"


def test_detector_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        WorkspaceDetector(threshold=1.5)


def test_candidate_exposes_matched_terms() -> None:
    ws = _workspace("ws-1", "Alpha Beta", "gamma")
    result = WorkspaceDetector(threshold=0.0).detect(_task("alpha beta"), [ws])
    assert isinstance(result.candidates[0], Candidate)
    assert set(result.candidates[0].matched_terms) == {"alpha", "beta"}
