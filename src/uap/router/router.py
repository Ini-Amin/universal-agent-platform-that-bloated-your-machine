"""Router: decides which domain workflow runs (Master section 5).

The Router does one thing only: map a TaskSpec's domain to a workflow. It never
plans, executes, researches, calls tools, or writes responses.
"""

from __future__ import annotations

from dataclasses import dataclass

from uap.contracts.models import Domain, TaskSpec

__all__ = ["DOMAIN_WORKFLOW_MAP", "Router", "RoutingDecision", "WorkflowRegistry"]


# The entire routing table. Adding a domain means adding one line here and
# registering its workflow -- no Router logic changes (Master section 30).
DOMAIN_WORKFLOW_MAP: dict[str, str] = {
    Domain.LEARNING: "LearningWorkflow",
    Domain.RESEARCH: "ResearchWorkflow",
    Domain.CODING: "CodingWorkflow",
    Domain.BBP: "BBPWorkflow",
    Domain.DATA: "DataWorkflow",
    Domain.UNKNOWN: "Clarification",
}

_CLARIFICATION = "Clarification"


@dataclass(frozen=True)
class RoutingDecision:
    """Where a TaskSpec was routed, and why."""

    domain: str
    workflow_name: str | None  # None when unmapped
    target: object | None  # registered workflow object, None when unmapped
    reason: str


class WorkflowRegistry:
    """Stores workflow objects by domain name.

    Deliberately domain-agnostic and free of workflow imports: it holds whatever
    object a caller registers, so the Router never depends on a workflow package
    (which would create a circular import).
    """

    def __init__(self) -> None:
        self._workflows: dict[str, object] = {}

    def register(self, domain: str, workflow: object) -> None:
        """Register ``workflow`` as the implementation for ``domain``."""
        self._workflows[str(domain)] = workflow

    def get(self, domain: str) -> object | None:
        """Return the registered workflow for ``domain``, or None."""
        return self._workflows.get(str(domain))

    def available(self) -> list[str]:
        """Domains that have a registered workflow."""
        return sorted(self._workflows)


class Router:
    """Maps a TaskSpec to the workflow that should execute it."""

    def __init__(self, registry: WorkflowRegistry | None = None) -> None:
        self.registry = registry if registry is not None else WorkflowRegistry()

    def route(self, spec: TaskSpec) -> RoutingDecision:
        """Resolve ``spec`` to a workflow. Never raises for a valid TaskSpec."""
        domain = (
            spec.domain.value if isinstance(spec.domain, Domain) else str(spec.domain)
        )
        workflow_name = DOMAIN_WORKFLOW_MAP.get(domain, _CLARIFICATION)
        target = self.registry.get(domain)
        reason = self._reason(domain, workflow_name, target)
        return RoutingDecision(
            domain=domain, workflow_name=workflow_name, target=target, reason=reason
        )

    @staticmethod
    def _reason(
        domain: str, workflow_name: str | None, target: object | None
    ) -> str:
        if target is None:
            return f"Domain '{domain}' is not bound to a live workflow"
        return f"Domain '{domain}' routed to {workflow_name}"
