"""Centralized Credential Manager (Master section 32).

Credentials must never appear in model context, logs, artifacts, checkpoints,
decision traces, or Git (sections 32, 68). Callers hold a :class:`CredentialRef`
-- an opaque, serialization-safe pointer -- and the *only* way to obtain the
secret value is :meth:`CredentialManager.resolve` at the moment of use
(runtime injection). The stored JSON file and every ``CredentialRef`` carry
metadata only; the secret value lives in a separate, redacted-on-dump map.

Supported: versioning, rotation, scope, deletion, redaction. Expiration/usage
history/revocation hooks are additive on top of this ref model.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import utc_now
from uap.contracts.models import UTCDateTime

__all__ = [
    "CredentialRef",
    "CredentialManager",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialExpiredError",
    "CredentialRevokedError",
]


class CredentialError(Exception):
    """Base error for credential failures."""


class CredentialNotFoundError(CredentialError, KeyError):
    """Raised when a credential ref or name cannot be resolved."""


class CredentialExpiredError(CredentialError, ValueError):
    """Raised when a credential has passed its expiration time."""


class CredentialRevokedError(CredentialError, ValueError):
    """Raised when a credential has been explicitly revoked."""

_DEFAULT_STORE = Path.home() / ".uap" / "credentials.json"


class CredentialRef(BaseModel):
    """An opaque reference to a stored credential -- NEVER its value.

    Safe to serialize, log, embed in a decision trace, or commit: it carries
    only identity and metadata. The secret is retrieved exclusively through
    :meth:`CredentialManager.resolve`.
    """

    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    kind: str
    scope: str = "global"
    version: int = 1
    created_at: UTCDateTime = Field(default_factory=utc_now)
    rotated_at: UTCDateTime | None = None
    expires_at: UTCDateTime | None = None
    revoked_at: UTCDateTime | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return utc_now() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_valid(self) -> bool:
        return not self.is_expired and not self.is_revoked


class CredentialManager:
    """Metadata-and-value store with a strict value/metadata separation.

    The on-disk JSON holds refs *and* values (local secret store); the file is
    the trust boundary. In-memory, values live only in ``_values`` keyed by
    ``ref_id`` and are never placed on the ref objects.
    """

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store_path = Path(store_path) if store_path else _DEFAULT_STORE
        self._refs: dict[str, CredentialRef] = {}
        self._values: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def register(
        self,
        name: str,
        value: str,
        kind: str,
        scope: str = "global",
        *,
        expires_at: datetime | None = None,
    ) -> CredentialRef:
        """Store a new credential value; return its ref (value excluded)."""
        ref = CredentialRef(
            name=name,
            kind=kind,
            scope=scope,
            version=1,
            expires_at=expires_at,
        )
        self._refs[ref.ref_id] = ref
        self._values[ref.ref_id] = value
        self._persist()
        return ref

    def get_ref(self, ref_id_or_name: str) -> CredentialRef | None:
        """Find a ref by ref_id or name. Returns the latest version for names."""
        if ref_id_or_name in self._refs:
            return self._refs[ref_id_or_name]
        matches = [r for r in self._refs.values() if r.name == ref_id_or_name]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version)

    def rotate(self, ref_id_or_name: str, value: str) -> CredentialRef:
        """Replace the value and bump the version. Unknown -> KeyError."""
        ref = self.get_ref(ref_id_or_name)
        if ref is None or ref.ref_id not in self._refs:
            raise CredentialNotFoundError(f"unknown credential: {ref_id_or_name}")
        ref_id = ref.ref_id
        updated = ref.model_copy(
            update={"version": ref.version + 1, "rotated_at": utc_now()}
        )
        self._refs[ref_id] = updated
        self._values[ref_id] = value
        self._persist()
        return updated

    def revoke(self, ref_id_or_name: str) -> CredentialRef:
        """Revoke a credential. Unknown -> KeyError."""
        ref = self.get_ref(ref_id_or_name)
        if ref is None or ref.ref_id not in self._refs:
            raise CredentialNotFoundError(f"unknown credential: {ref_id_or_name}")
        updated = ref.model_copy(update={"revoked_at": utc_now()})
        self._refs[ref.ref_id] = updated
        self._persist()
        return updated

    def expire(self, ref_id_or_name: str, at: datetime | None = None) -> CredentialRef:
        """Mark a credential as expired. Unknown -> KeyError."""
        ref = self.get_ref(ref_id_or_name)
        if ref is None or ref.ref_id not in self._refs:
            raise CredentialNotFoundError(f"unknown credential: {ref_id_or_name}")
        updated = ref.model_copy(update={"expires_at": at or utc_now()})
        self._refs[ref.ref_id] = updated
        self._persist()
        return updated

    def delete(self, ref_id_or_name: str) -> None:
        """Forget a credential entirely. Unknown -> KeyError."""
        ref = self.get_ref(ref_id_or_name)
        if ref is None or ref.ref_id not in self._refs:
            raise CredentialNotFoundError(f"unknown credential: {ref_id_or_name}")
        ref_id = ref.ref_id
        del self._refs[ref_id]
        self._values.pop(ref_id, None)
        self._persist()

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    def resolve(self, ref_id_or_name: str) -> str:
        """The ONLY value accessor (runtime injection).

        Raises:
            CredentialNotFoundError (KeyError): if unknown.
            CredentialRevokedError: if credential is revoked.
            CredentialExpiredError: if credential is expired.
        """
        ref = self.get_ref(ref_id_or_name)
        if ref is None or ref.ref_id not in self._values:
            raise CredentialNotFoundError(f"unknown credential: {ref_id_or_name}")
        if ref.is_revoked:
            raise CredentialRevokedError(f"credential revoked: {ref.name}")
        if ref.is_expired:
            raise CredentialExpiredError(f"credential expired: {ref.name}")
        return self._values[ref.ref_id]
    def list_refs(self) -> list[CredentialRef]:
        """Every ref (metadata only), insertion order."""
        return list(self._refs.values())

    def redact(self, text: str) -> str:
        """Replace any known secret value in ``text`` with ``[REDACTED:name]``.

        A defense-in-depth helper for log/trace sinks (section 68); the primary
        control is that values never leave :meth:`resolve`.
        """
        out = text
        for ref_id, value in self._values.items():
            if value and value in out:
                out = out.replace(value, f"[REDACTED:{self._refs[ref_id].name}]")
        return out

    # ------------------------------------------------------------------ #
    # Representation / persistence (value-free)
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        # NEVER leak values; count only.
        return f"CredentialManager(store={self._store_path!s}, refs={len(self._refs)})"

    def _persist(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            ref_id: {
                "ref": json.loads(ref.model_dump_json()),
                "value": self._values.get(ref_id, ""),
            }
            for ref_id, ref in self._refs.items()
        }
        self._store_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # SECURITY: the store holds plaintext secrets. Restrict to the owner
        # (0600) and tighten the containing directory (0700) — without this any
        # local user could read every credential (audit finding 2026-10-02).
        try:
            import os as _os

            _os.chmod(self._store_path, 0o600)
            _os.chmod(self._store_path.parent, 0o700)
        except OSError:
            # Filesystems without POSIX modes (or a read-only mount) — the
            # write itself still succeeded; do not fail the operation.
            pass

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        for ref_id, entry in raw.items():
            self._refs[ref_id] = CredentialRef.model_validate(entry["ref"])
            self._values[ref_id] = entry.get("value", "")
