"""Tests for the knowledge layer (Master sections 13, 14, 15, 41, 42, 63).

DB-backed tests run against the local PostgreSQL ``uap_test`` database and skip
cleanly when it is unreachable (same policy as ``tests/test_db_foundation.py``).
Embedding and model tests are pure and always run.

Knowledge is deliberately separate from Memory (SQLite, section 16) and from
Artifacts (filesystem, section 19): these tests exercise the new PG/pgvector
store, the deterministic embedder, and the policy-gated lifecycle.
"""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.contracts import (
    Artifact,
    VerificationCriterion,
    VerificationResult,
    utc_now,
)
from uap.db import Base, create_db_engine, create_session_factory, session_scope
from uap.knowledge import (
    EMBEDDING_DIM,
    HashingEmbedder,
    KnowledgeItem,
    KnowledgeLifecycle,
    KnowledgeStatus,
    KnowledgeStore,
    PolicyDeniedError,
    PromotionPolicy,
    Provenance,
    embed_texts,
)
from uap.knowledge.model import SOURCE_KINDS
from pydantic import ValidationError

# --------------------------------------------------------------------------- #
# Connection / skip handling (mirrors tests/test_db_foundation.py)
# --------------------------------------------------------------------------- #

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

#: Tables this feature owns, for per-test truncation.
_KNOWLEDGE_TABLES = (
    "knowledge_events",
    "knowledge_provenance",
    "knowledge_items",
)

# --------------------------------------------------------------------------- #
# Fixtures
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
    table_list = ", ".join(f'"{name}"' for name in _KNOWLEDGE_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

EMBEDDER = HashingEmbedder()

def _provenance(ref: str = "artifact-1", **overrides: object) -> Provenance:
    base: dict[str, object] = {
        "source_kind": "artifact",
        "source_ref": ref,
        "extracted_by": "recon-agent",
        "extracted_at": utc_now(),
        "evidence": "observed in output",
    }
    base.update(overrides)
    return Provenance(**base)  # type: ignore[arg-type]

def _item(**overrides: object) -> KnowledgeItem:
    base: dict[str, object] = {
        "statement": "the API exposes a GraphQL endpoint",
        "domain": "bbp",
        "provenance": [_provenance()],
    }
    base.update(overrides)
    return KnowledgeItem(**base)  # type: ignore[arg-type]

def _verification(passed: bool, notes: str | None = None) -> VerificationResult:
    return VerificationResult(
        verifier="evidence-verifier",
        passed=passed,
        criteria=[VerificationCriterion(criterion="source reachable", passed=passed)],
        notes=notes,
    )

def _reference_vector(text_value: str) -> list[float]:
    """Independent re-implementation of the HashingEmbedder algorithm.

    Uses ``sha256`` (never ``hash()``) so this matches across processes and
    proves the embedder is PYTHONHASHSEED-independent.
    """
    vector = [0.0] * EMBEDDING_DIM
    for token in text_value.lower().split():
        token = "".join(ch for ch in token if ch.isalnum())
        if not token:
            continue
        index = int.from_bytes(hashlib.sha256(token.encode()).digest(), "big") % EMBEDDING_DIM
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector

# =========================================================================== #
# 1. HashingEmbedder is deterministic and text-sensitive
# =========================================================================== #

def test_hashing_embedder_is_deterministic_and_text_sensitive() -> None:
    first = HashingEmbedder().embed("GraphQL endpoint exposed")
    second = HashingEmbedder().embed("GraphQL endpoint exposed")

    assert first == second
    # Independent sha256 recomputation -> proves stability across processes.
    assert first == _reference_vector("GraphQL endpoint exposed")
    assert first != HashingEmbedder().embed("completely different claim")

def test_embed_texts_batch_helper_preserves_order() -> None:
    texts = ["alpha beta", "gamma"]
    batched = embed_texts(EMBEDDER, texts)
    assert batched == [EMBEDDER.embed(text) for text in texts]

# =========================================================================== #
# 2. Dimension and L2 normalisation
# =========================================================================== #

def test_embedding_dimension_and_l2_norm() -> None:
    vector = EMBEDDER.embed("one two three four five")
    assert len(vector) == EMBEDDING_DIM == 384
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)

def test_embedding_empty_text_is_zero_vector() -> None:
    vector = EMBEDDER.embed("   !!!   ")
    assert vector == [0.0] * EMBEDDING_DIM

# =========================================================================== #
# 3. KnowledgeItem requires >= 1 provenance
# =========================================================================== #

def test_knowledge_item_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        KnowledgeItem(statement="unbacked claim", domain="d", provenance=[])

def test_provenance_rejects_unknown_source_kind() -> None:
    assert SOURCE_KINDS == ("artifact", "execution", "external", "human")
    with pytest.raises(ValidationError):
        _provenance(source_kind="telepathy")

# =========================================================================== #
# 4. add + get round-trip, including provenance rows
# =========================================================================== #

@requires_db
def test_add_and_get_round_trip(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        original = _item(
            statement="port 443 runs TLS",
            domain="research",
            confidence=0.77,
            tags=["tls", "network"],
            provenance=[_provenance("artifact-a"), _provenance("artifact-b")],
        )
        stored = store.add(original, EMBEDDER)

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        reloaded = store.get(stored.knowledge_id)
        assert reloaded is not None
        assert reloaded.statement == "port 443 runs TLS"
        assert reloaded.domain == "research"
        assert reloaded.confidence == pytest.approx(0.77)
        assert reloaded.tags == ["tls", "network"]
        assert len(reloaded.provenance) == 2
        assert {p.source_ref for p in reloaded.provenance} == {"artifact-a", "artifact-b"}
        assert all(p.extracted_by == "recon-agent" for p in reloaded.provenance)

@requires_db
def test_get_unknown_returns_none(session_factory) -> None:
    with session_scope(session_factory) as session:
        assert KnowledgeStore(session).get(str(uuid.uuid4())) is None

# =========================================================================== #
# 5. add writes a 'proposed' lifecycle event
# =========================================================================== #

@requires_db
def test_add_writes_proposed_event(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        item = store.add(_item(), EMBEDDER)

    with session_scope(session_factory) as session:
        events = KnowledgeStore(session).history(item.knowledge_id)
        assert [event["event"] for event in events] == ["proposed"]
        assert events[0]["actor"] == "recon-agent"

# =========================================================================== #
# 6. search returns the nearest item first
# =========================================================================== #

@requires_db
def test_search_returns_nearest_first(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        near = store.add(
            _item(statement="graphql introspection is enabled", domain="bbp"),
            EMBEDDER,
        )
        store.add(
            _item(statement="the cdn caches static assets", domain="bbp"),
            EMBEDDER,
        )

    with session_scope(session_factory) as session:
        results = KnowledgeStore(session).search(
            "graphql introspection enabled", embedder=EMBEDDER
        )
        assert results, "search returned nothing"
        assert results[0][0].knowledge_id == near.knowledge_id
        # Cosine distance: nearest first, ordered ascending.
        distances = [distance for _, distance in results]
        assert distances == sorted(distances)

# =========================================================================== #
# 7. search domain filter
# =========================================================================== #

@requires_db
def test_search_domain_filter(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        store.add(_item(statement="alpha endpoint", domain="bbp"), EMBEDDER)
        store.add(_item(statement="alpha endpoint", domain="research"), EMBEDDER)

    with session_scope(session_factory) as session:
        results = KnowledgeStore(session).search(
            "alpha endpoint", embedder=EMBEDDER, domain="research"
        )
        assert len(results) == 1
        assert results[0][0].domain == "research"

# =========================================================================== #
# 8. search status filter
# =========================================================================== #

@requires_db
def test_search_status_filter(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        kept = store.add(_item(statement="beta claim", domain="d"), EMBEDDER)
        moved = store.add(_item(statement="beta claim", domain="d"), EMBEDDER)
        store.update_status(
            moved.knowledge_id, KnowledgeStatus.REJECTED, actor="human"
        )

    with session_scope(session_factory) as session:
        results = KnowledgeStore(session).search(
            "beta claim", embedder=EMBEDDER, statuses=[KnowledgeStatus.PROPOSED]
        )
        ids = {item.knowledge_id for item, _ in results}
        assert kept.knowledge_id in ids
        assert moved.knowledge_id not in ids

# =========================================================================== #
# 9. search fallback for NULL-embedding rows (ILIKE path)
# =========================================================================== #

@requires_db
def test_search_falls_back_for_null_embedding(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        # No embedder -> embedding column stays NULL.
        unembedded = store.add(
            _item(statement="zebra unique marker phrase", domain="d")
        )

    with session_scope(session_factory) as session:
        results = KnowledgeStore(session).search("zebra unique marker", embedder=EMBEDDER)
        ids = {item.knowledge_id for item, _ in results}
        assert unembedded.knowledge_id in ids
        # The fallback hit is the raw text match.
        fallback = next(item for item, _ in results if item.knowledge_id == unembedded.knowledge_id)
        assert fallback.statement == "zebra unique marker phrase"

# =========================================================================== #
# 10. update_status writes an event and changes status
# =========================================================================== #

@requires_db
def test_update_status_writes_event(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        item = store.add(_item(), EMBEDDER)
        updated = store.update_status(
            item.knowledge_id,
            KnowledgeStatus.VERIFIED,
            actor="verifier",
            reason="criteria met",
        )
        assert updated.status == KnowledgeStatus.VERIFIED

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        reloaded = store.get(item.knowledge_id)
        assert reloaded is not None and reloaded.status == KnowledgeStatus.VERIFIED
        events = store.history(item.knowledge_id)
        assert [event["event"] for event in events] == ["proposed", "verified"]
        assert events[-1]["reason"] == "criteria met"

@requires_db
def test_update_status_unknown_raises(session_factory) -> None:
    with pytest.raises(LookupError):
        with session_scope(session_factory) as session:
            KnowledgeStore(session).update_status(
                str(uuid.uuid4()), KnowledgeStatus.VERIFIED, actor="x"
            )

# =========================================================================== #
# 11. supersede links old -> new; old becomes DEMOTED
# =========================================================================== #

@requires_db
def test_supersede_links_old_to_new(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        old = store.add(_item(statement="endpoint is v1"), EMBEDDER)
        new_item = _item(statement="endpoint is v2", confidence=0.95)
        created = store.supersede(old.knowledge_id, new_item, actor="human")
        assert created.supersedes == old.knowledge_id

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        old_reloaded = store.get(old.knowledge_id)
        # Chosen terminal state for a superseded item is DEMOTED; the
        # 'superseded' event preserves why/when/who (section 15 vocabulary has
        # no dedicated SUPERSEDED status).
        assert old_reloaded is not None
        assert old_reloaded.status == KnowledgeStatus.DEMOTED
        events = store.history(old.knowledge_id)
        assert events[-1]["event"] == "superseded"
        # The audit reason names the replacement item.
        assert created.knowledge_id in events[-1]["reason"]

# =========================================================================== #
# 12. history returns ordered events
# =========================================================================== #

@requires_db
def test_history_is_ordered(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        item = store.add(_item(), EMBEDDER)
        store.update_status(item.knowledge_id, KnowledgeStatus.VERIFIED, actor="v")
        store.update_status(item.knowledge_id, KnowledgeStatus.PROMOTED, actor="h")

    with session_scope(session_factory) as session:
        events = KnowledgeStore(session).history(item.knowledge_id)
        assert [event["event"] for event in events] == [
            "proposed",
            "verified",
            "promoted",
        ]
        assert [event["actor"] for event in events] == ["recon-agent", "v", "h"]

# =========================================================================== #
# 13. purge_expired removes only expired rows
# =========================================================================== #

@requires_db
def test_purge_expired_removes_only_expired(session_factory) -> None:
    now = utc_now()
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        expired = store.add(
            _item(statement="stale fact", expires_at=now - timedelta(days=1)),
            EMBEDDER,
        )
        live = store.add(
            _item(statement="fresh fact", expires_at=now + timedelta(days=1)),
            EMBEDDER,
        )
        no_expiry = store.add(_item(statement="evergreen fact"), EMBEDDER)

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        removed = store.purge_expired()
        assert removed == 1

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        assert store.get(expired.knowledge_id) is None
        assert store.get(live.knowledge_id) is not None
        assert store.get(no_expiry.knowledge_id) is not None

# =========================================================================== #
# 14. Lifecycle: propose -> verify -> promote (happy path)
# =========================================================================== #

@requires_db
def test_lifecycle_happy_path(session_factory) -> None:
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(KnowledgeStore(session))
        proposed = lifecycle.propose(
            _item(confidence=0.9), actor="recon-agent", embedder=EMBEDDER
        )
        assert proposed.status == KnowledgeStatus.PROPOSED

        verified = lifecycle.verify(
            proposed.knowledge_id, _verification(True, "sources agree"), actor="verifier"
        )
        assert verified.status == KnowledgeStatus.VERIFIED

        promoted = lifecycle.promote(proposed.knowledge_id, actor="human")
        assert promoted.status == KnowledgeStatus.PROMOTED

    with session_scope(session_factory) as session:
        events = [
            event["event"]
            for event in KnowledgeStore(session).history(proposed.knowledge_id)
        ]
        assert events == ["proposed", "verified", "promoted"]

# =========================================================================== #
# 15. promote below min_confidence -> PolicyDeniedError with reasons
# =========================================================================== #

@requires_db
def test_promote_below_min_confidence_denied(session_factory) -> None:
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(
            KnowledgeStore(session), PromotionPolicy(min_confidence=0.6)
        )
        item = lifecycle.propose(
            _item(confidence=0.2, domain="research"),
            actor="recon-agent",
            embedder=EMBEDDER,
        )
        lifecycle.verify(item.knowledge_id, _verification(True), actor="verifier")

    with pytest.raises(PolicyDeniedError) as excinfo:
        with session_scope(session_factory) as session:
            KnowledgeLifecycle(
                KnowledgeStore(session), PromotionPolicy(min_confidence=0.6)
            ).promote(item.knowledge_id, actor="human")

    reasons = excinfo.value.reasons
    assert any("confidence" in reason for reason in reasons)

    # The failed promotion must not have changed the status.
    with session_scope(session_factory) as session:
        reloaded = KnowledgeStore(session).get(item.knowledge_id)
        assert reloaded is not None
        assert reloaded.status == KnowledgeStatus.VERIFIED

# =========================================================================== #
# 16. promote without a passing verification -> denied
# =========================================================================== #

@requires_db
def test_promote_without_verification_denied(session_factory) -> None:
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(
            KnowledgeStore(session),
            PromotionPolicy(require_verification_pass=True),
        )
        item = lifecycle.propose(
            _item(confidence=0.95), actor="recon-agent", embedder=EMBEDDER
        )

    with pytest.raises(PolicyDeniedError) as excinfo:
        with session_scope(session_factory) as session:
            KnowledgeLifecycle(
                KnowledgeStore(session),
                PromotionPolicy(require_verification_pass=True),
            ).promote(item.knowledge_id, actor="human")

    assert any("verification" in reason for reason in excinfo.value.reasons)

@requires_db
def test_promote_allowed_domain_enforced(session_factory) -> None:
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(
            KnowledgeStore(session),
            PromotionPolicy(allowed_domains=["research"], require_verification_pass=False),
        )
        item = lifecycle.propose(
            _item(domain="bbp", confidence=0.9), actor="a", embedder=EMBEDDER
        )

    with pytest.raises(PolicyDeniedError) as excinfo:
        with session_scope(session_factory) as session:
            KnowledgeLifecycle(
                KnowledgeStore(session),
                PromotionPolicy(
                    allowed_domains=["research"], require_verification_pass=False
                ),
            ).promote(item.knowledge_id, actor="human")
    assert any("allowed_domains" in reason for reason in excinfo.value.reasons)

# =========================================================================== #
# 17. verify(passed=False) keeps PROPOSED and records the failure
# =========================================================================== #

@requires_db
def test_verify_failure_keeps_proposed_and_records(session_factory) -> None:
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(KnowledgeStore(session))
        item = lifecycle.propose(_item(confidence=0.9), actor="a", embedder=EMBEDDER)
        result = lifecycle.verify(
            item.knowledge_id,
            _verification(False, "source contradicted the claim"),
            actor="verifier",
        )
        assert result.status == KnowledgeStatus.PROPOSED

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        reloaded = store.get(item.knowledge_id)
        assert reloaded is not None and reloaded.status == KnowledgeStatus.PROPOSED
        events = store.history(item.knowledge_id)
        assert [event["event"] for event in events] == ["proposed", "proposed"]
        assert "failed" in events[-1]["reason"]
        assert "source contradicted the claim" in events[-1]["reason"]

# =========================================================================== #
# 18. propose_from_artifact derives provenance from the artifact
# =========================================================================== #

@requires_db
def test_propose_from_artifact_sets_provenance(session_factory) -> None:
    artifact = Artifact(
        task_id="task-42",
        type="report.md",
        source="research",
        uri="task-42/report.md",
        content_ref="task-42/report.md",
    )
    with session_scope(session_factory) as session:
        lifecycle = KnowledgeLifecycle(KnowledgeStore(session))
        item = lifecycle.propose_from_artifact(
            artifact,
            statement="the target exposes a staging host",
            domain="research",
            actor="recon-agent",
        )

    assert item.status == KnowledgeStatus.PROPOSED
    assert len(item.provenance) == 1
    provenance = item.provenance[0]
    assert provenance.source_kind == "artifact"
    assert provenance.source_ref == artifact.artifact_id
    assert provenance.extracted_by == "recon-agent"

    with session_scope(session_factory) as session:
        reloaded = KnowledgeStore(session).get(item.knowledge_id)
        assert reloaded is not None
        assert reloaded.provenance[0].source_ref == artifact.artifact_id

# =========================================================================== #
# 19. expire_overdue transitions VERIFIED items past expires_at to EXPIRED
# =========================================================================== #

@requires_db
def test_expire_overdue_transitions_verified_items(session_factory) -> None:
    past = utc_now() - timedelta(hours=1)
    future = utc_now() + timedelta(days=30)
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        lifecycle = KnowledgeLifecycle(store)
        overdue = lifecycle.propose(
            _item(statement="overdue claim", expires_at=past),
            actor="a",
            embedder=EMBEDDER,
        )
        lifecycle.verify(overdue.knowledge_id, _verification(True), actor="v")
        still_valid = lifecycle.propose(
            _item(statement="valid claim", expires_at=future),
            actor="a",
            embedder=EMBEDDER,
        )
        lifecycle.verify(still_valid.knowledge_id, _verification(True), actor="v")

    with session_scope(session_factory) as session:
        expired_count = KnowledgeLifecycle(KnowledgeStore(session)).expire_overdue()
        assert expired_count == 1

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        overdue_row = store.get(overdue.knowledge_id)
        valid_row = store.get(still_valid.knowledge_id)
        assert overdue_row is not None and overdue_row.status == KnowledgeStatus.EXPIRED
        assert valid_row is not None and valid_row.status == KnowledgeStatus.VERIFIED
        assert store.history(overdue.knowledge_id)[-1]["event"] == "expired"

@requires_db
def test_expire_overdue_ignores_already_terminal(session_factory) -> None:
    past = utc_now() - timedelta(days=1)
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        rejected = store.add(
            _item(statement="rejected claim", expires_at=past), EMBEDDER
        )
        store.update_status(rejected.knowledge_id, KnowledgeStatus.REJECTED, actor="h")

    with session_scope(session_factory) as session:
        assert KnowledgeLifecycle(KnowledgeStore(session)).expire_overdue() == 0
        reloaded = KnowledgeStore(session).get(rejected.knowledge_id)
        assert reloaded is not None
        assert reloaded.status == KnowledgeStatus.REJECTED

# =========================================================================== #
# Integration: list_by_status and single alembic head
# =========================================================================== #

@requires_db
def test_list_by_status(session_factory) -> None:
    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        store.add(_item(statement="one"), EMBEDDER)
        second = store.add(_item(statement="two"), EMBEDDER)
        store.update_status(second.knowledge_id, KnowledgeStatus.VERIFIED, actor="v")

    with session_scope(session_factory) as session:
        store = KnowledgeStore(session)
        assert len(store.list_by_status(KnowledgeStatus.PROPOSED)) == 1
        verified = store.list_by_status(KnowledgeStatus.VERIFIED)
        assert [item.knowledge_id for item in verified] == [second.knowledge_id]

def test_alembic_has_single_head_and_knowledge_migration() -> None:
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[1]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads}"

    migration_files = list((repo_root / "migrations" / "versions").glob("*.py"))
    assert any("knowledge" in path.name for path in migration_files)
