"""WebSocket status bridge: serve in-memory run records to the WS layer.

The Canvas UI creates tasks through ``POST /tasks``, which tracks them in the
server's in-memory ``runs`` dict (a :class:`RunRecord`). The WebSocket layer
(``uap.server.ws``) was built against the durable ``ExecutionService``
(PostgreSQL ``executions`` table) — so for a UI-created task it returned
``status: "unknown"`` and the canvas status badge stayed stuck at
``connecting`` (verified in a real browser, 2026-10-02).

:class:`CompositeRunService` closes that gap without touching either side:
it answers ``status()`` / ``events_since()`` from the durable service when the
execution exists there, and otherwise falls back to the in-memory run records
plus the observability event bus. The WS layer stays transport-only.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["CompositeRunService"]


class CompositeRunService:
    """Durable ``ExecutionService`` + in-memory runs behind one WS-facing API.

    Args:
        durable: the ``ExecutionService`` (may be ``None`` when PostgreSQL is
            unavailable — the server still runs with in-memory state only).
        runs_getter: callable returning the live ``runs`` dict
            (``{task_id: RunRecord}``) at call time (never captured).
        events_getter: callable returning a list of ``ObsEvent`` for a task id
            (e.g. ``MemorySink.query(task_id=...)``), or an empty list.
    """

    def __init__(
        self,
        durable: Any | None,
        runs_getter: Callable[[], dict[str, Any]],
        events_getter: Callable[[str], list[Any]] | None = None,
    ) -> None:
        self._durable = durable
        self._runs_getter = runs_getter
        self._events_getter = events_getter

    # ------------------------------------------------------------------ #
    # WS-facing API (mirrors ExecutionService's shape)
    # ------------------------------------------------------------------ #

    def status(self, execution_id: str) -> dict[str, Any]:
        """Return the status projection, preferring the durable record."""
        durable = self._durable_status(execution_id)
        if durable is not None:
            return durable

        record = self._runs_getter().get(execution_id)
        if record is not None:
            return {
                "execution_id": execution_id,
                "status": record.status,
                "resume_count": 0,
                "error": record.error,
                "output": record.output or None,
                "artifacts": [dict(item) for item in (record.artifacts or [])],
                "node_history": list(record.node_history or []),
            }

        return {"execution_id": execution_id, "status": "unknown"}

    def events_since(self, execution_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """Return events after ``after_seq``, durable first, in-memory fallback."""
        durable = self._durable_events(execution_id, after_seq)
        if durable:
            return durable

        if self._events_getter is None:
            return []

        events = self._events_getter(execution_id) or []
        out: list[dict[str, Any]] = []
        for index, event in enumerate(events, start=1):
            if index <= after_seq:
                continue
            out.append(
                {
                    "seq": index,
                    "kind": _event_attr(event, "kind"),
                    "node": _event_attr(event, "node"),
                    "payload": {
                        "workflow": _event_attr(event, "workflow"),
                        "agent": _event_attr(event, "agent"),
                        "duration_ms": _event_attr(event, "duration_ms"),
                        "error": _event_attr(event, "error"),
                        **(_event_attr(event, "data") or {}),
                    },
                    "ts": _iso(_event_attr(event, "ts")),
                }
            )
        return out

    def __getattr__(self, name: str) -> Any:
        """Delegate action methods (pause_request, resume_request, fork, ...)
        to the durable service so the WS control plane keeps working; the
        in-memory runs have no control plane of their own (yet).
        """
        durable = self.__dict__.get("_durable")
        if durable is not None and hasattr(durable, name):
            return getattr(durable, name)
        raise AttributeError(name)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _durable_status(self, execution_id: str) -> dict[str, Any] | None:
        if self._durable is None:
            return None
        try:
            return self._durable.status(execution_id)
        except Exception:
            return None

    def _durable_events(self, execution_id: str, after_seq: int) -> list[dict[str, Any]]:
        if self._durable is None or not hasattr(self._durable, "events_since"):
            return []
        try:
            return self._durable.events_since(execution_id, after_seq=after_seq)
        except Exception:
            return []


def _event_attr(event: Any, name: str) -> Any:
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)
