"""Multi-agent coordination control plane (Master section 24).

A *control plane*, not a boss agent: agents never call each other directly.
They exchange :class:`AgentMessage` over a :class:`MessageBus`, form a
:class:`Team` by capability, and delegate work through a
:class:`DelegationService` with a recursion guard (section 21 spirit).
"""

from uap.coordination.delegation import DelegationRequest, DelegationService
from uap.coordination.messaging import AgentMessage, MessageBus, MessageKind
from uap.coordination.team import Coordinator, Team, TeamBuilder, TeamRole

__all__ = [
    "AgentMessage",
    "Coordinator",
    "DelegationRequest",
    "DelegationService",
    "MessageBus",
    "MessageKind",
    "Team",
    "TeamBuilder",
    "TeamRole",
]
