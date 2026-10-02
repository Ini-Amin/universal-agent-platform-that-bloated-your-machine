"""Reproducible package manifest (Master section 50).

A :class:`PackageManifest` is the canonical description of an exported
workspace slice: the exact definition and graph versions it contains (each
pinned by a content hash), plus the runtime requirements needed to reproduce it.
Secrets are never serialised (section 50) -- only credential *references* belong
in ``dependencies``/configs upstream.

The manifest's own ``content_hash`` is the sha256 over the canonical JSON of
every other field, so a package can be re-hashed and verified byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts.models import UTCDateTime, utc_now

__all__ = ["PackageManifest", "canonical_json", "content_hash_of"]

_FORBID = ConfigDict(extra="forbid")


class PackageManifest(BaseModel):
    """The reproducible-package manifest (section 50)."""

    model_config = _FORBID

    package_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    created_at: UTCDateTime = Field(default_factory=utc_now)
    uap_version: str = "0.1.0"
    definitions: list[dict] = Field(default_factory=list)
    graphs: list[dict] = Field(default_factory=list)
    python_version: str
    dependencies: list[str] = Field(default_factory=list)
    schema_version: int = 1
    content_hash: str = ""


def canonical_json(payload: dict) -> str:
    """Deterministic JSON: sorted keys, UTF-8, compact separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash_of(manifest: PackageManifest) -> str:
    """Sha256 over the manifest's canonical JSON excluding ``content_hash``."""
    payload = manifest.model_dump(mode="json")
    payload.pop("content_hash", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
