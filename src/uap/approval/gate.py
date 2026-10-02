"""Explicit human-approval gate (Master section 20).

Side-effectful operations -- publishing, sending messages, destructive or
security-sensitive actions, production changes -- must pass through an explicit
workflow state (``PENDING_APPROVAL`` / ``APPROVED`` / ``REJECTED``) rather than
burying consent inside a prompt. The gate is the deterministic policy boundary:
LLM reasoning is never the only security control (Master section 29 rule 6).

State machine
-------------
``PENDING_APPROVAL`` is the only entry state. A single ``decide`` call moves it
to exactly one of ``APPROVED`` or ``REJECTED``, stamping ``decided_at`` (UTC)
and ``decided_by``. A second decision on the same request raises
``ApprovalError`` -- decisions are terminal and never silently overwritten.
"""

from __future__ import annotations

from uap.contracts.models import (
    ApprovalRequest,
    ApprovalState,
    WorkflowState,
    WorkflowStatus,
    utc_now,
)

__all__ = ["ApprovalError", "ApprovalGate"]

class ApprovalError(Exception):
    """Raised on an illegal approval transition or a failed requirement."""

class ApprovalGate:
    """In-memory registry of approval requests and their terminal decisions.

    The storage is deliberately simple (a dict keyed by ``approval_id``); a
    checkpoint/SQLite-backed gate can replace it behind the same interface
    without touching callers (Master section 25 upgrade path).
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    # ------------------------------------------------------------------ #
    # Creation and lookup
    # ------------------------------------------------------------------ #

    def request(
        self, task_id: str, action: str, details: dict | None = None
    ) -> ApprovalRequest:
        """Create and store a new ``PENDING_APPROVAL`` request."""
        req = ApprovalRequest(
            task_id=task_id,
            action=action,
            details=dict(details) if details is not None else {},
            state=ApprovalState.PENDING_APPROVAL,
        )
        self._requests[req.approval_id] = req
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        """Return the request, or None when the id is unknown."""
        return self._requests.get(approval_id)

    def _require_exists(self, approval_id: str) -> ApprovalRequest:
        req = self._requests.get(approval_id)
        if req is None:
            raise KeyError(approval_id)
        return req

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #

    def decide(
        self, approval_id: str, *, approved: bool, decided_by: str
    ) -> ApprovalRequest:
        """Resolve a pending request. Terminal: no double-decide."""
        req = self._require_exists(approval_id)
        if req.state is not ApprovalState.PENDING_APPROVAL:
            raise ApprovalError(
                f"approval {approval_id} already decided "
                f"({req.state.value}); decisions are terminal"
            )
        new_state = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
        updated = req.model_copy(
            update={
                "state": new_state,
                "decided_at": utc_now(),
                "decided_by": decided_by,
            }
        )
        self._requests[approval_id] = updated
        return updated

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def state(self, approval_id: str) -> ApprovalState:
        """Return the request's state. Unknown id raises KeyError."""
        return self._require_exists(approval_id).state

    def pending(self) -> list[ApprovalRequest]:
        """Every request still awaiting a decision."""
        return [
            req
            for req in self._requests.values()
            if req.state is ApprovalState.PENDING_APPROVAL
        ]

    # ------------------------------------------------------------------ #
    # Policy guards
    # ------------------------------------------------------------------ #

    def require(self, approval_id: str) -> ApprovalRequest:
        """Return the request only when it is APPROVED.

        This is the guard side-effectful code calls before acting: anything
        other than a positive human decision raises ``ApprovalError``.
        """
        req = self.get(approval_id)
        if req is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        if req.state is not ApprovalState.APPROVED:
            raise ApprovalError(
                f"approval {approval_id} is {req.state.value}, not approved"
            )
        return req

    # ------------------------------------------------------------------ #
    # Workflow wiring
    # ------------------------------------------------------------------ #

    def attach_to_state(
        self, state: WorkflowState, approval_id: str
    ) -> WorkflowState:
        """Attach a request to a workflow state and mark it awaiting approval.

        Returns a *new* ``WorkflowState`` (``model_copy``); the original is left
        untouched so checkpoint snapshots stay immutable.
        """
        req = self._require_exists(approval_id)
        return state.model_copy(
            update={
                "pending_approval": req,
                "status": WorkflowStatus.AWAITING_APPROVAL,
            }
        )
