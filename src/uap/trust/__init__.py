"""Agent-to-agent trust (Master section 25).

Never trust an agent result merely because another agent produced it. The
trust model records :class:`TrustClaim` statements, lets a verifier mark each
verified or failed, and maintains a bounded per-agent :class:`TrustRecord`
score reflecting that verification history.
"""

from uap.trust.model import TrustClaim, TrustRecord, TrustRegistry

__all__ = ["TrustClaim", "TrustRecord", "TrustRegistry"]
