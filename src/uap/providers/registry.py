"""Registry of bug bounty program providers."""

from __future__ import annotations

from typing import Iterable

from uap.providers.base import ProgramSource, ProviderStatus
from uap.providers.hackerone import HackerOneSource
from uap.providers.immunefi import ImmunefiSource
from uap.providers.yeswehack import YesWeHackSource


class ProviderRegistry:
    """Manages the available bug bounty provider implementations."""

    def __init__(self, sources: Iterable[ProgramSource] | None = None) -> None:
        self._sources: dict[str, ProgramSource] = {}
        if sources:
            for s in sources:
                self.register(s)

    def register(self, source: ProgramSource) -> None:
        self._sources[source.name.lower()] = source

    def get(self, name: str) -> ProgramSource | None:
        return self._sources.get(name.lower())

    def list_statuses(self) -> list[ProviderStatus]:
        """Return status and honest reason for every registered provider."""
        statuses: list[ProviderStatus] = []
        for src in self._sources.values():
            statuses.append(
                ProviderStatus(
                    name=src.name,
                    available=src.available(),
                    reason=src.unavailable_reason(),
                )
            )
        return statuses

    @classmethod
    def default(cls) -> ProviderRegistry:
        """Construct the default registry with all known platforms."""
        return cls([
            YesWeHackSource(),
            HackerOneSource(),
            ImmunefiSource(),
        ])


_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderRegistry.default()
    return _DEFAULT_REGISTRY
