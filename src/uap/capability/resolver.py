"""Capability resolver and scoped tokens (Master section 29).

The resolver is the deterministic authority between a capability *declaration*
and *execution*: it issues grants, revokes them at runtime, and answers
``check(subject, capability, scope)`` with an auditable ``(bool, reason)``.

For execution across a trust boundary it can mint a short-lived
**scoped token** -- a self-describing, HMAC-signed string that a downstream
executor validates without consulting the resolver. The signing secret is a
per-instance random value (``secrets.token_hex``); real deployments MUST inject
a stable secret (shared across processes, rotated out-of-band) via
``CapabilityResolver(secret=...)`` so tokens survive restarts and verify across
hosts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from uap.capability.model import CapabilityGrant
from uap.contracts import utc_now

__all__ = ["CapabilityResolver"]

_TOKEN_PREFIX = "uap"


class CapabilityResolver:
    """In-memory grant registry plus HMAC-signed scoped-token minting."""

    def __init__(self, secret: str | None = None) -> None:
        # Per-instance secret unless injected; see module docstring.
        self._secret: bytes = (secret or secrets.token_hex(32)).encode("utf-8")
        self._grants: dict[str, CapabilityGrant] = {}

    # ------------------------------------------------------------------ #
    # Grant lifecycle
    # ------------------------------------------------------------------ #

    def grant(
        self,
        subject: str,
        capability: str,
        scopes: list[str] | None = None,
        ttl_s: float | None = None,
        issued_by: str = "system",
    ) -> CapabilityGrant:
        """Issue (and store) a grant; ``ttl_s`` None means it never expires."""
        now = utc_now()
        g = CapabilityGrant(
            subject=subject,
            capability=capability,
            scopes=tuple(scopes or ()),
            issued_at=now,
            expires_at=(now + timedelta(seconds=ttl_s)) if ttl_s is not None else None,
            issued_by=issued_by,
        )
        self._grants[g.grant_id] = g
        return g

    def revoke(self, grant_id: str) -> CapabilityGrant:
        """Mark a grant revoked (runtime revocation, section 29). Unknown -> KeyError."""
        g = self._grants[grant_id]
        updated = g.model_copy(update={"revoked": True})
        self._grants[grant_id] = updated
        return updated

    def grants_for(self, subject: str) -> list[CapabilityGrant]:
        """Every grant issued to ``subject`` (any state), insertion order."""
        return [g for g in self._grants.values() if g.subject == subject]

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #

    def check(
        self,
        subject: str,
        capability: str,
        scope: str | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Fail-closed check. Returns ``(allowed, reason)``.

        A grant with empty ``scopes`` is a wildcard that covers any scope. The
        most permissive matching grant wins (any active grant allows).
        """
        now = now or utc_now()
        matches = [
            g
            for g in self._grants.values()
            if g.subject == subject and g.capability == capability
        ]
        if not matches:
            return False, f"no grant for {subject}/{capability}"

        # Prefer a reason from the "best" candidate: an active, scope-covering
        # grant allows; otherwise surface the most specific failure.
        scope_failure: str | None = None
        saw_active = False
        for g in matches:
            if not g.active(now):
                continue
            saw_active = True
            if not g.scopes:  # wildcard
                return True, "allowed"
            if scope is None or scope in g.scopes:
                return True, "allowed"
            scope_failure = f"scope {scope} not granted"

        if not saw_active:
            # Distinguish revoked from expired using the latest matching grant.
            latest = matches[-1]
            if latest.revoked:
                return False, "grant revoked"
            return False, "grant expired"
        return False, scope_failure or f"scope {scope} not granted"

    # ------------------------------------------------------------------ #
    # Scoped tokens
    # ------------------------------------------------------------------ #

    def issue_scoped_token(
        self,
        subject: str,
        capability: str,
        scope: str | None = None,
        ttl_s: float = 300,
    ) -> str:
        """Mint ``uap.<b64url(json)>.<hmac-sha256>`` for cross-boundary use."""
        now = utc_now()
        payload = {
            "subject": subject,
            "capability": capability,
            "scope": scope,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_s)).isoformat(),
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")
        sig = self._sign(body)
        return f"{_TOKEN_PREFIX}.{body}.{sig}"

    def check_token(self, token: str) -> tuple[bool, str, dict | None]:
        """Verify signature + expiry. Returns ``(valid, reason, payload|None)``."""
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
            return False, "malformed token", None
        _, body, sig = parts
        expected = self._sign(body)
        if not hmac.compare_digest(sig, expected):
            return False, "bad signature", None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
        except (ValueError, json.JSONDecodeError):
            return False, "malformed payload", None
        try:
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            return False, "malformed payload", None
        if utc_now() >= expires_at:
            return False, "token expired", payload
        return True, "valid", payload

    def _sign(self, body: str) -> str:
        return hmac.new(
            self._secret, body.encode("ascii"), hashlib.sha256
        ).hexdigest()
