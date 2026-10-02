"""Capability layer: declaration -> resolver -> scoped token (Master section 29).

This layer sits *above* the tool risk-tier guard (:mod:`uap.tools.policy`): the
tier guard decides whether a tool class may run, while capabilities bind a
*subject* to a *named permission* for a time window and narrow it to scopes.
"""

from __future__ import annotations

from uap.capability.model import Capability, CapabilityGrant
from uap.capability.resolver import CapabilityResolver

__all__ = ["Capability", "CapabilityGrant", "CapabilityResolver"]
