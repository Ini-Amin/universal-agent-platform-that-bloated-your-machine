"""Human approval public API (Master section 20) - build Step 12.

Explicit, auditable human decision points for side-effectful operations.
"""

from uap.approval.gate import ApprovalError, ApprovalGate

__all__ = ["ApprovalGate", "ApprovalError"]
