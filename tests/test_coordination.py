"""Tests for the multi-agent coordination control plane (Master sections 24, 25).

Deterministic and IO-free: fake agents stand in for real ones, a stub recorder
captures decision traces, and the message bus is exercised directly.
"""

from __future__ import annotations

import pytest

from uap.agents import AgentRegistry
from uap.agents.base import BaseAgent
from uap.contracts import AgentContext, AgentResult, AgentStatus
from uap.coordination import (
    AgentMessage,
    Coordinator,
    DelegationRequest,
    DelegationService,
    MessageBus,
    MessageKind,
    Team,
    TeamBuilder,
)
from uap.graph import GraphNode, NodeKind
from uap.trust import TrustRegistry


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Agent(BaseAgent):
    """Minimal agent whose name/capabilities are set per instance."""

    async def _timed_run(self, context: AgentContext) -> AgentResult:
        return AgentResult(agent_name=self.name, status=AgentStatus.SUCCESS, output="")


def _registry(*agents: tuple[str, list[str]]) -> AgentRegistry:
    reg = AgentRegistry()
    for name, caps in agents:
        reg.register(_Agent(name=name, capabilities=caps))
    return reg


def _msg(kind: MessageKind, sender: str, recipient: str, **body) -> AgentMessage:
    return AgentMessage(kind=kind, sender=sender, recipient=recipient, body=body)


# --------------------------------------------------------------------------- #
# 1-5. MessageBus
# --------------------------------------------------------------------------- #


def test_send_and_inbox_fifo():
    bus = MessageBus(_registry(("a", []), ("b", [])))
    bus.send(_msg(MessageKind.REQUEST, "a", "b", n=1))
    bus.send(_msg(MessageKind.REQUEST, "a", "b", n=2))
    inbox = bus.inbox("b")
    assert [m.body["n"] for m in inbox] == [1, 2]


def test_inbox_delivered_once():
    bus = MessageBus(_registry(("a", []), ("b", [])))
    bus.send(_msg(MessageKind.STATUS, "a", "b"))
    assert len(bus.inbox("b")) == 1
    assert bus.inbox("b") == []  # second read is empty - delivered-once


def test_peek_does_not_consume():
    bus = MessageBus(_registry(("a", []), ("b", [])))
    bus.send(_msg(MessageKind.STATUS, "a", "b", n=7))
    peeked = bus.peek("b")
    assert peeked is not None and peeked.body["n"] == 7
    # peek left it undelivered, so inbox still returns it
    assert len(bus.inbox("b")) == 1
    assert bus.peek("b") is None


def test_send_unknown_recipient_raises():
    bus = MessageBus(_registry(("a", [])))
    with pytest.raises(ValueError, match="unknown recipient"):
        bus.send(_msg(MessageKind.REQUEST, "a", "ghost"))


def test_conversation_returns_only_pair():
    bus = MessageBus(_registry(("a", []), ("b", []), ("c", [])))
    bus.send(_msg(MessageKind.REQUEST, "a", "b", k="ab"))
    bus.send(_msg(MessageKind.RESPONSE, "b", "a", k="ba"))
    bus.send(_msg(MessageKind.REQUEST, "a", "c", k="ac"))
    convo = bus.conversation("a", "b")
    assert [m.body["k"] for m in convo] == ["ab", "ba"]


# --------------------------------------------------------------------------- #
# 6-9. Team formation + coordinator resolution
# --------------------------------------------------------------------------- #


def test_team_builder_forms_roles_by_capability():
    reg = _registry(("recon", ["scan"]), ("writer", ["report", "scan"]))
    team = TeamBuilder(reg).form(
        "ops", [("scanner", ["scan"]), ("reporter", ["report"])]
    )
    assert isinstance(team, Team)
    assert team.agent_for_role("scanner") == "recon"
    assert team.agent_for_role("reporter") == "writer"


def test_team_builder_no_capable_agent_raises():
    reg = _registry(("recon", ["scan"]))
    with pytest.raises(ValueError, match=r"role 'exploiter'.*capability 'exploit'"):
        TeamBuilder(reg).form("ops", [("exploiter", ["exploit"])])


def test_team_builder_deterministic_registration_order():
    # Both agents cover 'scan'; the first-registered one must win.
    reg = _registry(("first", ["scan"]), ("second", ["scan"]))
    team = TeamBuilder(reg).form("ops", [("scanner", ["scan"])])
    assert team.agent_for_role("scanner") == "first"


def test_coordinator_resolve_role_then_agent_ref():
    reg = _registry(("recon", ["scan"]))
    team = TeamBuilder(reg).form("ops", [("scanner", ["scan"])])
    bus = MessageBus(reg)
    coord = Coordinator(bus, team, DelegationService(bus, reg))

    by_role = GraphNode(id="n1", kind=NodeKind.AGENT, config={"role": "scanner"})
    assert coord.resolve(by_role) == "recon"

    by_ref = GraphNode(id="n2", kind=NodeKind.AGENT, config={"agent_ref": "recon@v1"})
    assert coord.resolve(by_ref) == "recon@v1"


# --------------------------------------------------------------------------- #
# 10-11. Delegation
# --------------------------------------------------------------------------- #


async def test_delegation_sends_delegation_then_response():
    reg = _registry(("lead", []), ("worker", []))
    bus = MessageBus(reg)
    svc = DelegationService(bus, reg)

    async def runner(agent: str, payload: dict) -> dict:
        return {"did": agent, "echo": payload.get("x")}

    req = DelegationRequest(
        from_agent="lead", to_agent="worker", task="crunch", payload={"x": 42}
    )
    result = await svc.delegate(req, runner)

    assert result == {"did": "worker", "echo": 42}
    hist = bus.history()
    assert [m.kind for m in hist] == [MessageKind.DELEGATION, MessageKind.RESPONSE]
    assert hist[0].sender == "lead" and hist[0].recipient == "worker"
    assert hist[1].sender == "worker" and hist[1].recipient == "lead"
    assert hist[1].in_reply_to == hist[0].message_id


async def test_delegation_depth_guard_raises():
    reg = _registry(("lead", []), ("worker", []))
    svc = DelegationService(bus := MessageBus(reg), reg, max_delegation_depth=2)

    async def runner(agent: str, payload: dict) -> dict:
        return {}

    too_deep = DelegationRequest(
        from_agent="lead", to_agent="worker", task="t", max_depth=5
    )
    with pytest.raises(ValueError, match="recursion guard"):
        await svc.delegate(too_deep, runner)
    # nothing was posted on rejection
    assert bus.history() == []


# --------------------------------------------------------------------------- #
# 12-13. Trust
# --------------------------------------------------------------------------- #


def test_trust_verify_raises_and_lowers_with_bounds():
    reg = TrustRegistry()
    claim = reg.claim("alpha", "found an endpoint", ["artifact#1"])
    assert reg.record("alpha").trust_score == 0.5

    rec = reg.verify(claim.claim_id, verified=True)
    assert rec.verified_claims == 1
    assert rec.trust_score == pytest.approx(0.6)

    # Many failures floor at 0.0, not below.
    for _ in range(10):
        c = reg.claim("alpha", "noise")
        rec = reg.verify(c.claim_id, verified=False)
    assert rec.trust_score == 0.0

    # Many verifications cap at 1.0, not above.
    for _ in range(20):
        c = reg.claim("beta", "solid")
        rec = reg.verify(c.claim_id, verified=True)
    assert rec.trust_score == 1.0

    with pytest.raises(KeyError, match="unknown claim"):
        reg.verify("does-not-exist", verified=True)


def test_trust_rankings_ordered():
    reg = TrustRegistry()
    # high: beta (0.5 + 0.1), low: alpha (0.5 - 0.15), tie resolves by name asc.
    reg.verify(reg.claim("beta", "x").claim_id, verified=True)
    reg.verify(reg.claim("alpha", "y").claim_id, verified=False)
    reg.claim("gamma", "z")  # untouched -> 0.5
    ranked = reg.rankings()
    assert [r.agent for r in ranked] == ["beta", "gamma", "alpha"]
    assert ranked[0].trust_score > ranked[1].trust_score > ranked[2].trust_score
