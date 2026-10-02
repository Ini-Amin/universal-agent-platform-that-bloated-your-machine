"""Agent abstraction (Master section 10).

The interface is deliberately minimal: the orchestration layer coordinates
agents, agents only decide/act on a compiled ``AgentContext``. Agents must
never import UI, database internals, MCP details, LangGraph or HTTP layers
(Master section 10) - that boundary is enforced by tests/test_agents.py.
"""

import time
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from uap.contracts import AgentContext, AgentResult, AgentStatus


@runtime_checkable
class Agent(Protocol):
    """Minimal agent interface (Master section 10)."""

    name: str

    async def run(self, context: AgentContext) -> AgentResult:
        """Produce one result from one compiled context."""
        ...


class BaseAgent(ABC):
    """Shared scaffolding for concrete agents.

    Subclasses set the class-level ``name`` / ``capabilities`` defaults (or
    override them per instance via ``__init__``) and implement
    :meth:`_timed_run`. :meth:`run` measures wall-clock time, fills
    ``usage.latency_ms`` and converts any unexpected failure into a failed
    :class:`AgentResult` instead of letting it escape to the orchestrator.
    """

    name: str
    capabilities: list[str] = []

    def __init__(
        self, name: str | None = None, capabilities: list[str] | None = None
    ) -> None:
        resolved = name if name is not None else getattr(type(self), "name", None)
        if resolved is None or not resolved.strip():
            raise ValueError("agent name must be a non-empty string")
        self.name = resolved
        if capabilities is not None:
            self.capabilities = list(capabilities)

    @abstractmethod
    async def _timed_run(self, context: AgentContext) -> AgentResult:
        """Subclass hook: produce the result. Timing and errors are handled by run()."""

    async def run(self, context: AgentContext) -> AgentResult:
        start = time.perf_counter()
        try:
            result = await self._timed_run(context)
        except Exception as exc:  # agents never propagate failures upstream
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output="",
                error=f"{type(exc).__name__}: {exc}",
            )
        result.usage.latency_ms = (time.perf_counter() - start) * 1000.0
        return result
