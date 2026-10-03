"""YesWeHack bug bounty program provider."""

from __future__ import annotations

import logging
import time
from typing import Any
import httpx

from uap.providers.base import Program

logger = logging.getLogger(__name__)

DEFAULT_YESWEHACK_API = "https://api.yeswehack.com"
# ponytail: 300s TTL (5m) avoids provider rate limits and UI latency, update when webhook push exists.
DEFAULT_CACHE_TTL = 300.0


class YesWeHackSource:
    """Public bug bounty program source for YesWeHack (no credentials required)."""

    name: str = "yeswehack"

    def __init__(
        self,
        api_base: str = DEFAULT_YESWEHACK_API,
        cache_ttl: float = DEFAULT_CACHE_TTL,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.cache_ttl = cache_ttl
        self._cached_programs: list[Program] | None = None
        self._cache_expires_at: float = 0.0

    def available(self) -> bool:
        """YesWeHack programs API is public and does not require credentials."""
        return True

    def unavailable_reason(self) -> str:
        return "Public API available"

    def _fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch JSON payload with a sensible timeout."""
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={"User-Agent": "uap/1.0"})
            resp.raise_for_status()
            return resp.json()

    def list_programs(self, refresh: bool = False) -> list[Program]:
        """Fetch and return normalized YesWeHack programs, caching briefly."""
        now = time.monotonic()
        if not refresh and self._cached_programs is not None and now < self._cache_expires_at:
            return list(self._cached_programs)

        data = self._fetch_json(f"{self.api_base}/programs")
        items = data.get("items", []) if isinstance(data, dict) else []

        programs: list[Program] = []
        for item in items:
            slug = item.get("slug")
            if not slug:
                continue
            title = item.get("title") or slug
            bounty_max = item.get("bounty_reward_max")
            scopes_count = int(item.get("scopes_count", 0) or 0)

            tags: list[str] = []
            for field_name in ("activity_area", "country", "type"):
                val = item.get(field_name)
                if val:
                    tags.append(str(val))
            if item.get("bounty"):
                tags.append("bounty")
            if item.get("vdp"):
                tags.append("vdp")

            programs.append(
                Program(
                    id=slug,
                    name=title,
                    platform=self.name,
                    url=f"https://yeswehack.com/programs/{slug}",
                    bounty_max=bounty_max,
                    scope_count=scopes_count,
                    tags=tags,
                )
            )

        self._cached_programs = programs
        self._cache_expires_at = now + self.cache_ttl
        return list(programs)

    def get_primary_scope(self, program_id: str) -> str:
        """Resolve the primary target host from program scopes."""
        try:
            detail = self._fetch_json(f"{self.api_base}/programs/{program_id}")
            scopes = detail.get("scopes", []) if isinstance(detail, dict) else []
            for entry in scopes:
                raw_scope = entry.get("scope", "")
                if not raw_scope or not isinstance(raw_scope, str):
                    continue
                cleaned = raw_scope.strip()
                if "://" in cleaned:
                    cleaned = cleaned.split("://", 1)[1]
                if cleaned.startswith("*."):
                    cleaned = cleaned[2:]
                elif cleaned.startswith("."):
                    cleaned = cleaned[1:]
                if "/" in cleaned:
                    cleaned = cleaned.split("/", 1)[0]
                cleaned = cleaned.strip()
                if cleaned:
                    return cleaned
        except Exception as exc:
            logger.warning("Could not fetch detail for YesWeHack program '%s': %s", program_id, exc)

        return f"{program_id}.com"
