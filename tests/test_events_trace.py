"""Tests for the canonical runtime event model + decision trace (Master sections
21, 26, 45, 46, 73).

Event-model tests are pure (no DB). DB-backed tests run against the local
PostgreSQL ``uap_test`` database and skip cleanly when it is unreachable, the
same policy as ``tests/test_db_foundation.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory, session_scope
from uap.db.models import ExecutionCheckpoint, ExecutionStatus
from uap.db.repositories import DefinitionRepository, ExecutionRepository
from uap.events import (
    EVENTSET_VERSION,
    PAYLOAD_SCHEMAS,
    CanonicalEvent,
    EventType,
    event_types,
    validate_event,
)
from uap.trace import (
    DecisionAlternative,
    DecisionEvidence,
    DecisionRecorder,
    DecisionTrace,
    DecisionType,
    TraceStore,
    redact_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"

def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL

TEST_DATABASE_URL = _resolved_test_url()

def _reachable(url: str) -> bool:
    try:
        engine = create_db_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False

DATABASE_REACHABLE = _reachable(TEST_DATABASE_URL)

requires_db = pytest.mark.skipif(
    not DATABASE_REACHABLE,
    reason=f"PostgreSQL not reachable at {TEST_DATABASE_URL}",
)

#: Every table this feature owns, for per-test truncation.
_TABLES = (
    "decision_traces",
    "execution_checkpoints",
    "execution_events",
    "executions",
    "workflow_versions",
    "workflow_definitions",
)

# --------------------------------------------------------------------------- #
# DB fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def engine(isolated_engine: Engine) -> Engine:
    """The module's isolated schema (migrations applied by ``tests/conftest.py``)."""
    return isolated_engine

@pytest.fixture()
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield create_session_factory(engine)

@pytest.fixture(autouse=True)
def _truncate_between_tests(engine: Engine) -> Iterator[None]:
    yield
    table_list = ", ".join(f'"{name}"' for name in _TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))

def _make_execution(session: Session):
    repo = DefinitionRepository(session)
    definition = repo.create_definition(f"wf-{uuid.uuid4()}")
    version = repo.create_version(definition.id, {"nodes": ["a", "b"]})
    return ExecutionRepository(session).create(version.id, input={"x": 1})

# =========================================================================== #
# 1. EventType covers all section-45 categories
# =========================================================================== #

def test_event_type_covers_all_categories() -> None:
    members = list(EventType)
    assert len(members) >= 25

    names = {member.name for member in members}
    for prefix in (
        "EXECUTION_",
        "NODE_",
        "TOOL_",
        "APPROVAL_",
        "CHECKPOINT_",
        "KNOWLEDGE_",
        "EVALUATION_",
    ):
        assert any(name.startswith(prefix) for name in names), prefix

    # The canonical set is versioned (section 45: "structured and versioned").
    assert EVENTSET_VERSION >= 1
    # Values are lowercase snake_case strings; event_types() mirrors the enum.
    assert all(member.value == member.value.lower() for member in members)
    assert event_types() == [member.value for member in members]

# =========================================================================== #
# 2. CanonicalEvent JSON round-trip
# =========================================================================== #

def test_canonical_event_json_roundtrip() -> None:
    event = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        execution_id="exec-1",
        node_id="node-1",
        payload={"tool": "search", "args_summary": {"q": "x"}},
        correlation_id="corr-1",
        parent_event_id="evt-0",
    )
    restored = CanonicalEvent.model_validate_json(event.model_dump_json())
    assert restored == event
    assert restored.schema_version == 1
    assert restored.ts.tzinfo is not None

# =========================================================================== #
# 3-5. validate_event
# =========================================================================== #

def test_validate_event_missing_required_key_is_error() -> None:
    event = CanonicalEvent(
        event_type=EventType.MODEL_SELECTED,
        execution_id="e1",
        payload={"model": "gpt"},  # missing "reason"
    )
    violations = validate_event(event)
    assert len(violations) == 1
    assert violations[0].startswith("error:")
    assert "reason" in violations[0]

def test_validate_event_unknown_key_is_warning() -> None:
    event = CanonicalEvent(
        event_type=EventType.MODEL_SELECTED,
        execution_id="e1",
        payload={"model": "gpt", "reason": "cheapest", "bogus": 1},
    )
    violations = validate_event(event)
    assert len(violations) == 1
    assert violations[0].startswith("warning:")
    assert "bogus" in violations[0]

def test_validate_event_valid_is_empty() -> None:
    event = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        execution_id="e1",
        payload={"tool": "search", "args_summary": {"q": "x"}},
    )
    assert validate_event(event) == []

# =========================================================================== #
# 6. PAYLOAD_SCHEMAS covers every EventType
# =========================================================================== #

def test_payload_schemas_cover_every_event_type() -> None:
    assert set(PAYLOAD_SCHEMAS) == set(EventType)
    for event_type, spec in PAYLOAD_SCHEMAS.items():
        assert set(spec) == {"required", "optional"}, event_type
        assert isinstance(spec["required"], set)
        assert isinstance(spec["optional"], set)
        # A schema must require at least one key and never double-declare.
        assert spec["required"]
        assert not (spec["required"] & spec["optional"])

# =========================================================================== #
# 7. DecisionTrace JSON round-trip
# =========================================================================== #

def test_decision_trace_json_roundtrip() -> None:
    trace = DecisionTrace(
        execution_id="exec-1",
        node_id="node-1",
        decision_type=DecisionType.MODEL_SELECTION,
        chosen="gpt",
        alternatives=[DecisionAlternative(option="claude", reason_rejected="cost")],
        rationale="cheapest capable model",
        evidence=[DecisionEvidence(kind="artifact", ref="artifact-183")],
        confidence=0.8,
        inputs_summary={"tokens": 100},
    )
    restored = DecisionTrace.model_validate_json(trace.model_dump_json())
    assert restored == trace
    assert restored.trace_id == trace.trace_id
    assert restored.created_at.tzinfo is not None

def test_decision_trace_confidence_out_of_range_rejected() -> None:
    with pytest.raises(Exception):
        DecisionTrace(
            execution_id="e1",
            decision_type=DecisionType.ROUTING,
            chosen="research",
            rationale="only option",
            confidence=1.5,
        )

# =========================================================================== #
# 8. TraceStore.record + get round-trip
# =========================================================================== #

@requires_db
def test_trace_store_record_and_get_roundtrip(session_factory) -> None:
    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        trace = DecisionTrace(
            execution_id=str(execution.id),
            node_id="node-1",
            decision_type=DecisionType.TOOL_SELECTION,
            chosen="search",
            alternatives=[DecisionAlternative(option="scrape", reason_rejected="slow")],
            rationale="fastest adequate tool",
            evidence=[DecisionEvidence(kind="event", ref="evt-1")],
            confidence=0.6,
            inputs_summary={"goal": "find"},
        )
        TraceStore(session).record(trace)
        execution_id = execution.id

    with session_scope(session_factory) as session:
        loaded = TraceStore(session).get(trace.trace_id)
        assert loaded is not None
        assert loaded.execution_id == str(execution_id)
        assert loaded.decision_type is DecisionType.TOOL_SELECTION
        assert loaded.chosen == "search"
        assert loaded.confidence == 0.6
        assert loaded.alternatives[0].option == "scrape"
        assert loaded.evidence[0].ref == "evt-1"

# =========================================================================== #
# 9. list_for_execution ordered by created_at, limit respected
# =========================================================================== #

@requires_db
def test_list_for_execution_ordered_and_limited(session_factory) -> None:
    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        store = TraceStore(session)
        for index in range(5):
            store.record(
                DecisionTrace(
                    execution_id=str(execution.id),
                    decision_type=DecisionType.CONDITION,
                    chosen=f"branch-{index}",
                    rationale="deterministic branch",
                )
            )
        execution_id = execution.id

    with session_scope(session_factory) as session:
        traces = TraceStore(session).list_for_execution(str(execution_id))
        assert [t.chosen for t in traces] == [f"branch-{i}" for i in range(5)]
        created = [t.created_at for t in traces]
        assert created == sorted(created)

        limited = TraceStore(session).list_for_execution(str(execution_id), limit=2)
        assert [t.chosen for t in limited] == ["branch-0", "branch-1"]

# =========================================================================== #
# 10. FK cascade: deleting the execution deletes its traces
# =========================================================================== #

@requires_db
def test_deleting_execution_cascades_to_traces(session_factory) -> None:
    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        TraceStore(session).record(
            DecisionTrace(
                execution_id=str(execution.id),
                decision_type=DecisionType.SYNTHESIS,
                chosen="summary",
                rationale="single output",
            )
        )
        execution_id = execution.id

    with session_scope(session_factory) as session:
        session.execute(
            text("DELETE FROM executions WHERE id = :eid"), {"eid": execution_id}
        )

    with session_scope(session_factory) as session:
        remaining = session.execute(
            text("SELECT count(*) FROM decision_traces WHERE execution_id = :eid"),
            {"eid": execution_id},
        ).scalar_one()
        assert remaining == 0

# =========================================================================== #
# 11. DecisionRecorder emits DECISION_RECORDED with matching ids
# =========================================================================== #

@requires_db
def test_decision_recorder_emits_decision_recorded_event(session_factory) -> None:
    captured: list[CanonicalEvent] = []
    recorder = DecisionRecorder(session_factory, event_sink=captured.append)

    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        execution_id = execution.id

    trace = DecisionTrace(
        execution_id=str(execution_id),
        decision_type=DecisionType.ROUTING,
        chosen="research",
        rationale="domain detected",
    )
    returned = recorder.record(trace)

    assert len(captured) == 1
    event = captured[0]
    assert event.event_type is EventType.DECISION_RECORDED
    assert event.execution_id == str(execution_id)
    assert event.payload["trace_id"] == trace.trace_id
    assert event.payload["decision_type"] == DecisionType.ROUTING.value
    assert event.payload["chosen"] == "research"
    assert validate_event(event) == []

    # The trace is durable, not merely emitted.
    with session_scope(session_factory) as session:
        assert TraceStore(session).get(trace.trace_id) is not None
    assert returned.trace_id == trace.trace_id

# =========================================================================== #
# 12. Checkpoint UNIQUE(execution_id, seq)
# =========================================================================== #

@requires_db
def test_checkpoint_unique_execution_seq_enforced(session_factory) -> None:
    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        session.add(
            ExecutionCheckpoint(
                execution_id=execution.id, seq=1, node_id="a", state={"n": 1}
            )
        )
        session.add(
            ExecutionCheckpoint(
                execution_id=execution.id, seq=2, node_id="b", state={"n": 2}
            )
        )
        execution_id = execution.id

    with session_scope(session_factory) as session:
        count = session.execute(
            text("SELECT count(*) FROM execution_checkpoints WHERE execution_id = :eid"),
            {"eid": execution_id},
        ).scalar_one()
        assert count == 2

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(
                ExecutionCheckpoint(
                    execution_id=execution_id, seq=1, node_id="dup", state={}
                )
            )

# =========================================================================== #
# 13. executions.locked_by / heartbeat_at exist and are writable
# =========================================================================== #

@requires_db
def test_execution_lease_columns_writable(session_factory) -> None:
    from uap.contracts import utc_now
    from uap.db.models import Execution

    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        execution.locked_by = "worker-7"
        execution.heartbeat_at = utc_now()
        execution_id = execution.id

    with session_scope(session_factory) as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        assert row.locked_by == "worker-7"
        assert row.heartbeat_at is not None

# =========================================================================== #
# 14. Alembic: upgrade head -> downgrade -1 -> upgrade head round-trips
# =========================================================================== #

@requires_db
def test_alembic_roundtrip_and_single_head(engine: Engine) -> None:
    schema = f"trace_rt_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        url = _schema_url(schema)
        _alembic(url, "upgrade", "head")
        tables = set(sa_inspect(engine).get_table_names(schema=schema))
        assert {"decision_traces", "execution_checkpoints"} <= tables

        columns = {
            col["name"]
            for col in sa_inspect(engine).get_columns("executions", schema=schema)
        }
        assert {"locked_by", "heartbeat_at"} <= columns

        _alembic(url, "downgrade", "9e4d6114bbc6")
        downgraded = set(sa_inspect(engine).get_table_names(schema=schema))
        assert "decision_traces" not in downgraded
        assert "execution_checkpoints" not in downgraded

        _alembic(url, "upgrade", "head")
        again = set(sa_inspect(engine).get_table_names(schema=schema))
        assert {"decision_traces", "execution_checkpoints"} <= again
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

    # Exactly one head in the migration chain (revision-specific assertions above).
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1

def _schema_url(schema: str) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    return url.render_as_string(hide_password=False)

def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.attributes["sqlalchemy.url"] = url
    getattr(command, action)(config, revision)

# =========================================================================== #
# 15. inputs_summary redaction guard
# =========================================================================== #

def test_redact_secrets_helper() -> None:
    result = redact_secrets(
        {
            "api_key": "sk-live-123",
            "TOKEN": "abc",
            "password": "hunter2",
            "credential_id": "cred-9",
            "client_secret": "shh",
            "safe": "visible",
            "nested": {"db_password": "pw", "keep": 1},
            "items": [{"auth_token": "t"}, {"keep": 2}],
        }
    )
    for key in ("api_key", "TOKEN", "password", "credential_id", "client_secret"):
        assert result[key] == "***", key
    assert result["safe"] == "visible"
    assert result["nested"]["db_password"] == "***"
    assert result["nested"]["keep"] == 1
    assert result["items"][0]["auth_token"] == "***"
    assert result["items"][1]["keep"] == 2

    # Non-mapping input is defensively returned empty (never leaks).
    assert redact_secrets("raw") == {}

@requires_db
def test_decision_recorder_applies_redaction_before_persisting(session_factory) -> None:
    recorder = DecisionRecorder(session_factory)
    with session_scope(session_factory) as session:
        execution = _make_execution(session)
        execution_id = execution.id

    trace = DecisionTrace(
        execution_id=str(execution_id),
        decision_type=DecisionType.POLICY_CHECK,
        chosen="deny",
        rationale="credential exposure",
        inputs_summary={
            "api_key": "sk-live-secret",
            "endpoint": "https://example.test",
            "nested": {"access_token": "tok-123"},
        },
    )
    recorder.record(trace)

    with session_scope(session_factory) as session:
        stored = TraceStore(session).get(trace.trace_id)
        assert stored is not None
        assert stored.inputs_summary["api_key"] == "***"
        assert stored.inputs_summary["nested"]["access_token"] == "***"
        assert stored.inputs_summary["endpoint"] == "https://example.test"

    # And the raw DB row carries no secret bytes.
    with session_scope(session_factory) as session:
        raw = session.execute(
            text("SELECT inputs_summary FROM decision_traces WHERE id = :tid"),
            {"tid": trace.trace_id},
        ).scalar_one()
    assert "sk-live-secret" not in json.dumps(raw)
    assert "tok-123" not in json.dumps(raw)
