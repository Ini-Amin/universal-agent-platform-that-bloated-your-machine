"""Delegation service (Master sections 21, 24).

Delegation is the only way one agent gets work done by another: the requesting
agent hands a :class:`DelegationRequest` to the service, which posts a
``DELEGATION`` message on the bus, invokes an *injected* runner to actually run
the target agent, then posts a ``RESPONSE`` back. The runner injection keeps
agents decoupled - no agent imports or calls another. A depth guard enforces
the recursion limit (section 21 spirit).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from uap.agents import AgentRegistry
from uap.coordination.messaging import AgentMessage, MessageBus, MessageKind

__all__ = ["DelegationRequest", "DelegationService"]

_FORBID = ConfigDict(extra="forbid")


class DelegationRequest(BaseModel):
    """A request for ``to_agent`` to perform ``task`` on behalf of ``from_agent``."""

    model_config = _FORBID

    from_agent: str
    to_agent: str
    task: str
    payload: dict = Field(default_factory=dict)
    max_depth: int = 3


class DelegationService:
    """Routes delegation requests through the bus and an injected runner."""

    def __init__(
        self,
        bus: MessageBus,
        registry: AgentRegistry,
        *,
        max_delegation_depth: int = 3,
    ) -> None:
        self._bus = bus
        self._registry = registry
        self._max_delegation_depth = max_delegation_depth

    async def delegate(
        self,
        request: DelegationRequest,
        runner: Callable[[str, dict], Awaitable[dict]],
    ) -> dict:
        """Delegate ``request`` to its target agent via ``runner``.

        Posts a ``DELEGATION`` message, awaits ``runner(to_agent, payload)``,
        posts a ``RESPONSE`` back, and returns the runner's result. Rejects a
        request whose ``max_depth`` exceeds the service's configured recursion
        limit (recursion guard, section 21).
        """
        if request.max_depth > self._max_delegation_depth:
            raise ValueError(
                f"delegation depth {request.max_depth} exceeds max "
                f"{self._max_delegation_depth} (recursion guard, section 21)"
            )

        delegation = AgentMessage(
            kind=MessageKind.DELEGATION,
            sender=request.from_agent,
            recipient=request.to_agent,
            subject=request.task,
            body=dict(request.payload),
        )
        self._bus.send(delegation)

        result = await runner(request.to_agent, dict(request.payload))

        self._bus.send(
            AgentMessage(
                kind=MessageKind.RESPONSE,
                sender=request.to_agent,
                recipient=request.from_agent,
                subject=request.task,
                body=dict(result),
                in_reply_to=delegation.message_id,
            )
        )
        return result
