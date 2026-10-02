"""Tests for the Router (Master section 5): routing only, no execution."""

from __future__ import annotations

import pytest

from uap.contracts.models import Domain, TaskSpec
from uap.router import DOMAIN_WORKFLOW_MAP, Router, RoutingDecision, WorkflowRegistry


class FakeWorkflow:
    """Stand-in for a real workflow; the registry holds arbitrary objects."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<FakeWorkflow {self.name}>"


def _full_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    for domain, name in DOMAIN_WORKFLOW_MAP.items():
        if name != "Clarification":
            registry.register(domain, FakeWorkflow(name))
    return registry


def _spec(domain: Domain) -> TaskSpec:
    return TaskSpec(goal="g", input={}, constraints={}, domain=domain)


def _future_spec(domain: str) -> TaskSpec:
    """A spec for a domain not yet in the Domain enum (Master section 30).

    Adding a real domain grows the enum, which makes validation accept the value;
    we bypass validation to simulate that state without touching contracts.
    """
    return TaskSpec.model_construct(domain=domain, goal="g", input={}, constraints={})


# --------------------------------------------------------------------------- #
# 1. Known domains route to the right workflow name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        (Domain.LEARNING, "LearningWorkflow"),
        (Domain.RESEARCH, "ResearchWorkflow"),
        (Domain.CODING, "CodingWorkflow"),
        (Domain.BBP, "BBPWorkflow"),
        (Domain.DATA, "DataWorkflow"),
    ],
)
def test_known_domain_routes_to_expected_workflow(
    domain: Domain, expected: str
) -> None:
    decision = Router(_full_registry()).route(_spec(domain))
    assert decision.workflow_name == expected
    assert decision.domain == domain.value


# --------------------------------------------------------------------------- #
# 2. Unknown domain -> Clarification, no target, non-empty reason
# --------------------------------------------------------------------------- #


def test_unknown_domain_falls_back_to_clarification() -> None:
    decision = Router(_full_registry()).route(_spec(Domain.UNKNOWN))
    assert decision.workflow_name == "Clarification"
    assert decision.target is None
    assert decision.reason


def test_unmapped_domain_falls_back_to_clarification() -> None:
    decision = Router(_full_registry()).route(_future_spec("gamedev"))
    assert decision.workflow_name == "Clarification"
    assert decision.target is None
    assert decision.reason


# --------------------------------------------------------------------------- #
# 3. The registered object comes back by identity
# --------------------------------------------------------------------------- #


def test_registered_workflow_returned_as_target() -> None:
    registry = WorkflowRegistry()
    workflow = FakeWorkflow("LearningWorkflow")
    registry.register(Domain.LEARNING, workflow)

    decision = Router(registry).route(_spec(Domain.LEARNING))
    assert decision.target is workflow


# --------------------------------------------------------------------------- #
# 4. Known but unregistered domain -> name set, target None
# --------------------------------------------------------------------------- #


def test_known_domain_without_registration_has_no_target() -> None:
    decision = Router().route(_spec(Domain.CODING))
    assert decision.workflow_name == "CodingWorkflow"
    assert decision.target is None
    assert decision.reason


# --------------------------------------------------------------------------- #
# 5. registry.available() lists registered names
# --------------------------------------------------------------------------- #


def test_available_lists_registered_domains() -> None:
    registry = _full_registry()
    assert registry.available() == sorted(
        d for d, n in DOMAIN_WORKFLOW_MAP.items() if n != "Clarification"
    )


def test_empty_registry_lists_nothing() -> None:
    assert WorkflowRegistry().available() == []


def test_registry_get_returns_none_for_unregistered() -> None:
    assert WorkflowRegistry().get(Domain.DATA) is None


# --------------------------------------------------------------------------- #
# 6. Default registry works without error
# --------------------------------------------------------------------------- #


def test_default_registry_routes_without_error() -> None:
    router = Router()
    assert router.registry.available() == []
    decision = router.route(_spec(Domain.RESEARCH))
    assert isinstance(decision, RoutingDecision)
    assert decision.workflow_name == "ResearchWorkflow"


# --------------------------------------------------------------------------- #
# 7. A future domain is added by table entry + registration only
# --------------------------------------------------------------------------- #


def test_new_domain_added_without_router_change(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = FakeWorkflow("GameDevelopmentWorkflow")
    monkeypatch.setitem(DOMAIN_WORKFLOW_MAP, "gamedev", "GameDevelopmentWorkflow")

    registry = WorkflowRegistry()
    registry.register("gamedev", workflow)

    decision = Router(registry).route(_future_spec("gamedev"))
    assert decision.workflow_name == "GameDevelopmentWorkflow"
    assert decision.target is workflow
    assert decision.domain == "gamedev"
    assert decision.reason


# --------------------------------------------------------------------------- #
# 8. route() is pure: same spec in, same decision out
# --------------------------------------------------------------------------- #


def test_route_is_pure() -> None:
    router = Router(_full_registry())
    spec = _spec(Domain.BBP)

    first = router.route(spec)
    second = router.route(spec)

    assert first == second
    for field in ("domain", "workflow_name", "target", "reason"):
        assert getattr(first, field) == getattr(second, field)


def test_routing_decision_is_immutable() -> None:
    decision = Router().route(_spec(Domain.DATA))
    with pytest.raises(Exception):
        decision.target = FakeWorkflow("x")  # type: ignore[misc]


def test_route_leaves_the_spec_untouched() -> None:
    registry = _full_registry()
    spec = _spec(Domain.LEARNING)
    before = spec.model_copy(deep=True)

    Router(registry).route(spec)

    assert spec == before
