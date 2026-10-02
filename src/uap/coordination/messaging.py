"""Agent-to-agent message bus (Master section 24).

The bus is the coordination control plane's transport: agents post
:class:`AgentMessage` records addressed to one another instead of calling each
other directly. Delivery is FIFO per recipient with *delivered-once* semantics
- :meth:`MessageBus.inbox` advances a per-agent cursor so a message is read
exactly once, while :meth:`MessageBus.peek` is non-consuming.

The bus validates recipients against an injected :class:`AgentRegistry` so a
message can never be addressed to an agent that does not exist.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from uap.agents import AgentRegistry
from uap.contracts import UTCDateTime, utc_now

__all__ = ["AgentMessage", "MessageBus", "MessageKind"]

_FORBID = ConfigDict(extra="forbid")


class MessageKind(StrEnum):
    """The kinds of control-plane message agents exchange (section 24)."""

    REQUEST = "request"
    RESPONSE = "response"
    DELEGATION = "delegation"
    STATUS = "status"
    HANDOFF = "handoff"
    ESCALATION = "escalation"


class AgentMessage(BaseModel):
    """One addressed message on the coordination control plane."""

    model_config = _FORBID

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: MessageKind
    sender: str
    recipient: str
    subject: str = ""
    body: dict = Field(default_factory=dict)
    in_reply_to: str | None = None
    ts: UTCDateTime = Field(default_factory=utc_now)


class MessageBus:
    """FIFO, deliver-once message bus validated against an agent registry."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry
        self._log: list[AgentMessage] = []
        # per-recipient read cursor into ``_log`` (count delivered so far).
        self._cursor: dict[str, int] = {}

    def send(self, message: AgentMessage) -> None:
        """Append ``message``; the recipient must be a registered agent."""
        if self._registry.get(message.recipient) is None:
            raise ValueError(f"unknown recipient: {message.recipient!r}")
        self._log.append(message)

    def inbox(self, agent: str) -> list[AgentMessage]:
        """Return undelivered messages for ``agent`` (FIFO) and mark delivered."""
        delivered = self._cursor.get(agent, 0)
        pending = [m for m in self._log if m.recipient == agent]
        fresh = pending[delivered:]
        self._cursor[agent] = len(pending)
        return fresh

    def peek(self, agent: str) -> AgentMessage | None:
        """Return the next undelivered message for ``agent`` without consuming."""
        delivered = self._cursor.get(agent, 0)
        pending = [m for m in self._log if m.recipient == agent]
        if delivered >= len(pending):
            return None
        return pending[delivered]

    def conversation(self, agent_a: str, agent_b: str) -> list[AgentMessage]:
        """Messages exchanged between ``agent_a`` and ``agent_b``, in order."""
        pair = {agent_a, agent_b}
        return [m for m in self._log if {m.sender, m.recipient} == pair]

    def history(self) -> list[AgentMessage]:
        """Every message ever sent, in send order."""
        return list(self._log)
