"""Policy layer: Allow/Ask/Deny decisions (Master section 31).

The "Approval Boundary" of the capability pipeline (section 29). Distinct from
:mod:`uap.tools.policy`, which gates tool *risk tiers*; this engine evaluates
arbitrary ``(subject, action, resource)`` triples.
"""

from __future__ import annotations

from uap.policy.engine import PolicyEffect, PolicyEngine, PolicyRule

__all__ = ["PolicyEffect", "PolicyRule", "PolicyEngine"]
