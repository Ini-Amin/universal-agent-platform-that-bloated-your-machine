"""Immunefi bug bounty program provider."""

from __future__ import annotations

from uap.providers.base import Program, ProviderUnavailableError

IMMUNEFI_UNAVAILABLE_REASON = (
    "Immunefi does not offer a public JSON API (HTML only); web scraping is intentionally omitted."
)


class ImmunefiSource:
    """Immunefi bug bounty program source (honestly declared unavailable)."""

    name: str = "immunefi"

    def available(self) -> bool:
        """Immunefi is unavailable because it lacks a public JSON API."""
        return False

    def unavailable_reason(self) -> str:
        return IMMUNEFI_UNAVAILABLE_REASON

    def list_programs(self, refresh: bool = False) -> list[Program]:
        """Raise an honest exception rather than returning an empty list."""
        raise ProviderUnavailableError(self.unavailable_reason())

    def get_primary_scope(self, program_id: str) -> str:
        """Raise an honest exception rather than fabricating a scope."""
        raise ProviderUnavailableError(self.unavailable_reason())
