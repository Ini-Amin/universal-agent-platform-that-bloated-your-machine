"""Credential layer: references, never values (Master section 32).

Callers hold :class:`CredentialRef` objects (serialization-safe) and obtain the
real secret only via :meth:`CredentialManager.resolve` at point of use.
"""

from __future__ import annotations

from uap.credentials.manager import (
    CredentialError,
    CredentialExpiredError,
    CredentialManager,
    CredentialNotFoundError,
    CredentialRef,
    CredentialRevokedError,
)

__all__ = [
    "CredentialRef",
    "CredentialManager",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialExpiredError",
    "CredentialRevokedError",
]
