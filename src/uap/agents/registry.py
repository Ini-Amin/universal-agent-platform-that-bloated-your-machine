"""Agent registry: name lookup plus capability-based selection (Master section 10).

Capability selection lets a workflow ask for a kind of agent rather than a
specific one, mirroring the Model Router pattern in Master section 24.
"""

from uap.agents.base import Agent


class AgentRegistry:
    """Insertion-ordered registry of agents with capability-based selection."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        """Add an agent; duplicate names are rejected."""
        if agent.name in self._agents:
            raise ValueError(f"agent already registered: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> Agent | None:
        """Return the registered agent for name, or None."""
        return self._agents.get(name)

    def names(self) -> list[str]:
        """Registered names in registration order."""
        return list(self._agents)

    def select(self, capability: str) -> list[Agent]:
        """Agents declaring capability, in registration order."""
        return [
            agent
            for agent in self._agents.values()
            if capability in getattr(agent, "capabilities", ())
        ]
