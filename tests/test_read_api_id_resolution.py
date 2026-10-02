"""Read-API id resolution: a client-facing id must reach the same row.

The UI only ever knows the id ``POST /tasks`` handed it. The server stores that
as ``correlation_id``; the ``executions`` row has its own primary key. Every
read API must accept either form.

The regression this guards against was live: the WS layer asked for events by
correlation id, ``read_since`` matched zero rows, the bridge silently fell back
to the in-memory sink (2 coarse events), and the Events panel showed 2 rows
while PostgreSQL held 23. The canvas looked fine because ``_node_statuses``
in ``server/app.py`` already merged both sources — only the panel was starved.
"""

from __future__ import annotations

import uuid

import pytest

from uap.db.engine import get_session_factory
from uap.runtime.service import ExecutionService


def _service() -> ExecutionService:
    def _resolver(ref: str):
        raise KeyError(ref)

    class _NodeRuntime:
        async def run_node(self, *args, **kwargs):  # pragma: no cover - unused
            return {}

    return ExecutionService(
        get_session_factory(),
        graph_resolver=_resolver,
        node_runtime=_NodeRuntime(),
    )


def _known_execution() -> tuple[str, str] | None:
    """Return ``(row_id, correlation_id)`` for a completed execution, or None."""
    from uap.db.engine import session_scope
    from uap.db.models import Execution

    with session_scope() as session:
        row = (
            session.query(Execution)
            .filter(Execution.correlation_id.isnot(None))
            .order_by(Execution.created_at.desc())
            .first()
        )
        if row is None:
            return None
        return str(row.id), str(row.correlation_id)


def test_events_resolve_by_correlation_id() -> None:
    """``events_since`` must accept the client-facing id, not just the row id."""
    known = _known_execution()
    if known is None:
        pytest.skip("no execution with a correlation_id in this database")
    row_id, correlation_id = known

    service = _service()
    by_row = service.events_since(row_id)
    by_correlation = service.events_since(correlation_id)

    assert by_correlation == by_row, (
        "events_since(correlation_id) returned a different log than "
        "events_since(row_id); the read API is not resolving the alias"
    )


def test_status_resolves_by_correlation_id() -> None:
    """``status`` must accept either id form."""
    known = _known_execution()
    if known is None:
        pytest.skip("no execution with a correlation_id in this database")
    row_id, correlation_id = known

    service = _service()
    assert service.status(correlation_id)["status"] == service.status(row_id)["status"]


def test_unknown_id_returns_empty_not_foreign_events() -> None:
    """An unknown id must yield nothing — never another execution's events."""
    service = _service()
    assert service.events_since(str(uuid.uuid4())) == []


def test_checkpoints_resolve_by_correlation_id() -> None:
    """``checkpoints`` must accept either id form."""
    known = _known_execution()
    if known is None:
        pytest.skip("no execution with a correlation_id in this database")
    row_id, correlation_id = known

    service = _service()
    assert len(service.checkpoints(correlation_id)) == len(service.checkpoints(row_id))
