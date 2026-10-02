"""Dynamic team formation and graph-driven coordination (Master section 24).

:class:`TeamBuilder` fills typed :class:`TeamRole` slots by *capability
coverage* against an :class:`AgentRegistry`, deterministically picking the
first registered agent whose capabilities cover every requirement. A
:class:`Coordinator` then runs a team through a graph by resolving each AGENT
node to a concrete agent - by team role when the node names a ``role``,
otherwise by its ``agent_ref``. The coordinator never lets one agent invoke
another directly; cross-agent work goes through the :class:`DelegationService`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from uap.agents import AgentRegistry
from uap.contracts import UTCDateTime, utc_now
from uap.graph import GraphNode

if TYPE_CHECKING:  # avoid an import cycle; only needed for typing
    from uap.coordination.delegation import DelegationService
    from uap.coordination.messaging import MessageBus

__all__ = ["Coordinator", "Team", "TeamBuilder", "TeamRole"]

_FORBID = ConfigDict(extra="forbid")


class TeamRole(BaseModel):
    """A filled role: a named slot bound to a concrete agent."""

    model_config = _FORBID

    name: str
    agent_name: str
    required_capabilities: list[str] = Field(default_factory=list)


class Team(BaseModel):
    """A formed team: an ordered set of filled roles."""

    model_config = _FORBID

    team_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    roles: list[TeamRole]
    formed_at: UTCDateTime = Field(default_factory=utc_now)

    def agent_for_role(self, role_name: str) -> str | None:
        """Return the agent bound to ``role_name``, or ``None``."""
        for role in self.roles:
            if role.name == role_name:
                return role.agent_name
        return None


class TeamBuilder:
    """Forms teams by matching role capabilities to registered agents."""

    def __init__(self, agents: AgentRegistry) -> None:
        self._agents = agents

    def form(self, name: str, roles: list[tuple[str, list[str]]]) -> Team:
        """Build a :class:`Team` from ``(role_name, required_capabilities)`` pairs.

        Each role is filled by the first registered agent (registration order)
        whose capabilities cover *all* required ones. If no agent qualifies the
        method raises ``ValueError`` naming the role and the missing capability.
        """
        filled: list[TeamRole] = []
        for role_name, required in roles:
            picked = self._pick(required)
            if picked is None:
                missing = self._first_missing(required)
                raise ValueError(
                    f"no agent for role {role_name!r}: missing capability "
                    f"{missing!r}"
                )
            filled.append(
                TeamRole(
                    name=role_name,
                    agent_name=picked,
                    required_capabilities=list(required),
                )
            )
        return Team(name=name, roles=filled)

    def _pick(self, required: list[str]) -> str | None:
        want = set(required)
        for agent_name in self._agents.names():
            agent = self._agents.get(agent_name)
            have = set(getattr(agent, "capabilities", ()))
            if want <= have:
                return agent_name
        return None

    def _first_missing(self, required: list[str]) -> str:
        """A capability no registered agent declares (for the error message)."""
        covered: set[str] = set()
        for agent_name in self._agents.names():
            agent = self._agents.get(agent_name)
            covered |= set(getattr(agent, "capabilities", ()))
        for cap in required:
            if cap not in covered:
                return cap
        # Every capability exists somewhere, but no single agent covers all.
        return required[0] if required else ""


class Coordinator:
    """Runs a team through a graph, resolving AGENT nodes to concrete agents.

    Resolution is by team role when a node's ``config['role']`` names a role,
    otherwise by its ``config['agent_ref']``. Agents never call one another
    directly: cross-agent work is routed through :class:`DelegationService`.
    """

    def __init__(
        self,
        bus: "MessageBus",
        team: Team,
        delegation: "DelegationService",
    ) -> None:
        self.bus = bus
        self.team = team
        self.delegation = delegation

    def resolve(self, node: GraphNode) -> str:
        """Resolve ``node`` to a concrete agent name.

        ``config['role']`` wins (looked up on the team); otherwise
        ``config['agent_ref']`` is used verbatim. Raises ``ValueError`` when
        neither yields an agent.
        """
        role = node.config.get("role")
        if role is not None:
            agent = self.team.agent_for_role(role)
            if agent is None:
                raise ValueError(
                    f"node {node.id!r}: role {role!r} is not on team "
                    f"{self.team.name!r}"
                )
            return agent
        agent_ref = node.config.get("agent_ref")
        if agent_ref is not None:
            return agent_ref
        raise ValueError(
            f"node {node.id!r}: no 'role' or 'agent_ref' in config to resolve"
        )
