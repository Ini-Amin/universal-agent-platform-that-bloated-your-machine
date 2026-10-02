"""Capability domain model (Master section 29).

A :class:`Capability` is a *named permission* a tool or agent may request
(filesystem, network, process, credentials, external APIs). A
:class:`CapabilityGrant` binds a capability to a subject for a window of time,
optionally narrowed to a set of scopes. The grant is the auditable record the
Capability Resolver (:mod:`uap.capability.resolver`) checks before any
privileged action runs; capabilities are never escalated silently (section 68).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import utc_now
from uap.contracts.models import UTCDateTime

__all__ = ["Capability", "CapabilityGrant"]

_MODEL_CONFIG = ConfigDict(extra="forbid")


class Capability(BaseModel):
    """A named permission with a risk tier (0 local .. 3 side-effectful)."""

    model_config = _MODEL_CONFIG

    name: str
    description: str = ""
    risk_tier: int = Field(default=0, ge=0, le=3)
    scopes: tuple[str, ...] = ()


class CapabilityGrant(BaseModel):
    """A time-boxed, optionally scoped binding of a capability to a subject.

    ``scopes`` empty means *wildcard* -- the grant covers every scope of the
    capability. A grant is only effective while :meth:`active` returns True.
    """

    model_config = _MODEL_CONFIG

    grant_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subject: str
    capability: str
    scopes: tuple[str, ...] = ()
    issued_at: UTCDateTime = Field(default_factory=utc_now)
    expires_at: UTCDateTime | None = None
    issued_by: str = "system"
    revoked: bool = False

    def active(self, now: datetime | None = None) -> bool:
        """True when the grant is neither revoked nor expired at ``now``."""
        if self.revoked:
            return False
        if self.expires_at is None:
            return True
        return (now or utc_now()) < self.expires_at
