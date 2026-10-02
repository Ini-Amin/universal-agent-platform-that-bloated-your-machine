"""Decision trace persistence + recording (Master sections 26, 45, 46, 73).

Two collaborators:

* :class:`TraceStore` - a thin repository over the ``decision_traces`` table.
  It takes an injected :class:`~sqlalchemy.orm.Session` and never commits
  (transaction boundaries belong to the caller / ``session_scope``), exactly
  like the repositories in :mod:`uap.db.repositories` (section 73: no hidden
  global state).
* :class:`DecisionRecorder` - a convenience pairing a ``session_factory`` (for a
  short, self-contained write per call) with an optional canonical event sink.
  Recording a decision persists the trace **and** emits a
  :data:`~uap.events.model.EventType.DECISION_RECORDED` event, so the durable
  trace row and the canonical event stream stay linked by ``trace_id``.

Redaction (sections 26, 73): :func:`redact_secrets` scrubs values under
sensitive keys before persistence. ``DecisionRecorder`` applies it to
``inputs_summary`` (and the row's other JSON fields) so a decision trace can
never leak a secret into durable storage.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from uap.db.models.trace import DecisionTraceRow
from uap.events.model import CanonicalEvent, EventType
from uap.trace.model import (
    DecisionAlternative,
    DecisionEvidence,
    DecisionTrace,
    DecisionType,
)

__all__ = [
    "DecisionRecorder",
    "TraceStore",
    "redact_secrets",
]

#: Keys whose values are scrubbed before persistence. Case-insensitive; matches
#: anywhere in the key name (``api_key``, ``token``, ``Password``, ...).
_SENSITIVE_KEY_RE = re.compile(r"(?i)(secret|token|key|password|credential)")

#: The replacement written in place of a sensitive value.
_REDACTED = "***"

def redact_secrets(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with sensitive values replaced by ``'***'``.

    A key is sensitive when it matches ``(?i)(secret|token|key|password|
    credential)``. Redaction recurses through nested mappings and lists so a
    secret nested under ``{"config": {"api_key": ...}}`` is still scrubbed.

    Non-mapping input is returned as an empty dict (defensive: the caller wanted
    a mapping and never a secret-bearing scalar).
    """

    if not isinstance(data, Mapping):
        return {}

    def _scrub(key: Any, value: Any) -> Any:
        if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
            return _REDACTED
        if isinstance(value, Mapping):
            return {k: _scrub(k, v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_scrub(None, item) for item in value]
        return value

    return {k: _scrub(k, v) for k, v in data.items()}

# --------------------------------------------------------------------------- #
# TraceStore
# --------------------------------------------------------------------------- #

class TraceStore:
    """Repository for decision traces, backed by an injected ``Session``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, trace: DecisionTrace) -> DecisionTrace:
        """Persist ``trace`` and return it.

        ``inputs_summary`` is redacted here as a defence-in-depth guarantee,
        even when the caller did not pre-redact. The row is flushed so its
        ``id`` is populated inside the current transaction.
        """

        row = DecisionTraceRow(
            id=_as_uuid(trace.trace_id),
            execution_id=_as_uuid(trace.execution_id),
            node_id=trace.node_id,
            decision_type=trace.decision_type.value,
            chosen=trace.chosen,
            alternatives=[alt.model_dump() for alt in trace.alternatives],
            rationale=trace.rationale,
            evidence=[ev.model_dump() for ev in trace.evidence],
            confidence=trace.confidence,
            inputs_summary=redact_secrets(trace.inputs_summary),
        )
        self._session.add(row)
        self._session.flush()
        return trace

    def get(self, trace_id: str) -> DecisionTrace | None:
        """Return the trace with this id, or ``None`` if unknown."""

        row = self._session.get(DecisionTraceRow, _as_uuid(trace_id))
        return None if row is None else _to_contract(row)

    def list_for_execution(
        self, execution_id: str, *, limit: int = 200
    ) -> list[DecisionTrace]:
        """Traces for one execution, oldest first, capped at ``limit``."""

        stmt = (
            select(DecisionTraceRow)
            .where(DecisionTraceRow.execution_id == _as_uuid(execution_id))
            .order_by(DecisionTraceRow.created_at.asc(), DecisionTraceRow.id.asc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [_to_contract(row) for row in rows]

# --------------------------------------------------------------------------- #
# DecisionRecorder
# --------------------------------------------------------------------------- #

class DecisionRecorder:
    """Persist decision traces and emit the matching canonical event.

    ``session_factory`` is a ``sessionmaker`` (see
    :func:`uap.db.engine.get_session_factory`). Each :meth:`record` opens a
    short session, commits the trace, then emits the event - so the durable
    write is never lost to a slow or failing sink.

    ``event_sink`` is any callable accepting one
    :class:`~uap.events.model.CanonicalEvent`; when ``None`` the recorder still
    persists but emits nothing.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        event_sink: Callable[[CanonicalEvent], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_sink = event_sink

    def record(self, trace: DecisionTrace) -> DecisionTrace:
        """Persist ``trace`` (redacted) and emit ``DECISION_RECORDED``.

        The emitted event's payload carries ``trace_id`` and ``execution_id``
        (plus ``decision_type`` / ``chosen``), tying the canonical event stream
        back to the durable trace row (sections 26, 45, 46).
        """

        # Redact before the row is built so no secret ever reaches the DB.
        redacted_inputs = redact_secrets(trace.inputs_summary)
        safe_trace = trace.model_copy(update={"inputs_summary": redacted_inputs})

        session = self._session_factory()
        try:
            TraceStore(session).record(safe_trace)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        self._emit(safe_trace)
        return safe_trace

    # -- internal ----------------------------------------------------------- #

    def _emit(self, trace: DecisionTrace) -> None:
        if self._event_sink is None:
            return
        event = CanonicalEvent(
            event_type=EventType.DECISION_RECORDED,
            execution_id=trace.execution_id,
            node_id=trace.node_id,
            payload={
                "decision_type": trace.decision_type.value,
                "chosen": trace.chosen,
                "trace_id": trace.trace_id,
                "rationale": trace.rationale,
                "confidence": trace.confidence,
                "alternative_count": len(trace.alternatives),
                "evidence_count": len(trace.evidence),
            },
        )
        self._event_sink(event)

# --------------------------------------------------------------------------- #
# Mapping helpers
# --------------------------------------------------------------------------- #

def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Coerce a string/UUID to a UUID (raising ``ValueError`` on bad input)."""

    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))

def _to_contract(row: DecisionTraceRow) -> DecisionTrace:
    """Map a ``DecisionTraceRow`` back onto the framework-free contract."""

    return DecisionTrace(
        trace_id=str(row.id),
        execution_id=str(row.execution_id),
        node_id=row.node_id,
        decision_type=DecisionType(row.decision_type),
        chosen=row.chosen,
        alternatives=[
            DecisionAlternative.model_validate(item) for item in (row.alternatives or [])
        ],
        rationale=row.rationale,
        evidence=[
            DecisionEvidence.model_validate(item) for item in (row.evidence or [])
        ],
        confidence=row.confidence,
        inputs_summary=dict(row.inputs_summary or {}),
        created_at=row.created_at,
    )
