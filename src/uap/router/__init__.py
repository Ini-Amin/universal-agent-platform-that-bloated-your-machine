"""Router package public API (Master section 5)."""

from uap.router.router import (
    DOMAIN_WORKFLOW_MAP,
    Router,
    RoutingDecision,
    WorkflowRegistry,
)

__all__ = ["DOMAIN_WORKFLOW_MAP", "Router", "RoutingDecision", "WorkflowRegistry"]
