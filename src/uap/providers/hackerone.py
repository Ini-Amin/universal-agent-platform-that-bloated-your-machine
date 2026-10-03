"""HackerOne bug bounty program provider."""

from __future__ import annotations

import logging
import os
import time
from typing import Any
import httpx

from uap.providers.base import Program, ProviderUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_HACKERONE_API = "https://api.hackerone.com/v1/hackers"
DEFAULT_CACHE_TTL = 300.0
HACKERONE_UNAVAILABLE_REASON = (
    "HackerOne needs an API token; set HACKERONE_API_USERNAME and HACKERONE_API_TOKEN"
)


class HackerOneSource:
    """HackerOne bug bounty program provider requiring HTTP Basic auth."""

    name: str = "hackerone"

    def __init__(
        self,
        api_base: str = DEFAULT_HACKERONE_API,
        cache_ttl: float = DEFAULT_CACHE_TTL,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.cache_ttl = cache_ttl
        self._cached_programs: list[Program] | None = None
        self._cache_expires_at: float = 0.0

    def _get_credentials(self) -> tuple[str, str] | None:
        user = os.environ.get("HACKERONE_API_USERNAME", "").strip()
        token = os.environ.get("HACKERONE_API_TOKEN", "").strip()
        if user and token:
            return user, token
        return None

    def available(self) -> bool:
        """True if HACKERONE_API_USERNAME and HACKERONE_API_TOKEN are set."""
        return self._get_credentials() is not None

    def unavailable_reason(self) -> str:
        """Honest degradation message when credentials are not configured."""
        if not self.available():
            return HACKERONE_UNAVAILABLE_REASON
        return "Credentials configured"

    def _fetch_json(self, url: str) -> dict[str, Any]:
        creds = self._get_credentials()
        if not creds:
            raise ProviderUnavailableError(HACKERONE_UNAVAILABLE_REASON)
        user, token = creds
        # ponytail: standard Basic auth, update to OAuth bearer if HackerOne personal tokens migrate.
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                auth=(user, token),
                headers={"Accept": "application/json", "User-Agent": "uap/1.0"},
            )
            resp.raise_for_status()
            return resp.json()

    def list_programs(self, refresh: bool = False) -> list[Program]:
        """Fetch normalized HackerOne programs or degrade honestly."""
        if not self.available():
            raise ProviderUnavailableError(HACKERONE_UNAVAILABLE_REASON)

        now = time.monotonic()
        if not refresh and self._cached_programs is not None and now < self._cache_expires_at:
            return list(self._cached_programs)

        data = self._fetch_json(f"{self.api_base}/programs")
        items = data.get("data", []) if isinstance(data, dict) else []

        programs: list[Program] = []
        for item in items:
            attrs = item.get("attributes", {})
            handle = attrs.get("handle") or item.get("id")
            if not handle:
                continue
            name = attrs.get("name") or handle
            url = attrs.get("url") or f"https://hackerone.com/{handle}"
            scopes_data = (
                item.get("relationships", {})
                .get("structured_scopes", {})
                .get("data", [])
            )
            scope_count = len(scopes_data) if isinstance(scopes_data, list) else 0

            tags: list[str] = []
            if attrs.get("offers_bounties"):
                tags.append("bounty")
            if attrs.get("offers_swag"):
                tags.append("swag")
            if attrs.get("submission_state"):
                tags.append(str(attrs["submission_state"]))

            programs.append(
                Program(
                    id=handle,
                    name=name,
                    platform=self.name,
                    url=url,
                    bounty_max=None,
                    scope_count=scope_count,
                    tags=tags,
                )
            )

        self._cached_programs = programs
        self._cache_expires_at = now + self.cache_ttl
        return list(programs)

    def get_primary_scope(self, program_id: str) -> str:
        """Resolve primary target for HackerOne program."""
        if not self.available():
            raise ProviderUnavailableError(HACKERONE_UNAVAILABLE_REASON)

        try:
            detail = self._fetch_json(f"{self.api_base}/programs/{program_id}")
            relationships = detail.get("data", {}).get("relationships", {})
            scopes = relationships.get("structured_scopes", {}).get("data", [])
            for s in scopes:
                asset = s.get("attributes", {}).get("asset_identifier", "").strip()
                if asset:
                    if "://" in asset:
                        asset = asset.split("://", 1)[1]
                    if asset.startswith("*."):
                        asset = asset[2:]
                    if "/" in asset:
                        asset = asset.split("/", 1)[0]
                    return asset.strip()
        except Exception as exc:
            logger.warning("Could not fetch detail for HackerOne program '%s': %s", program_id, exc)

        return f"{program_id}.com"
