"""Base models, protocols, and exceptions for bug bounty program providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from pydantic import BaseModel, Field


class Program(BaseModel):
    """Normalized provider-neutral bug bounty program representation."""

    id: str
    name: str
    platform: str
    url: str
    bounty_max: float | int | None = None
    scope_count: int = 0
    tags: list[str] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    """Status and honest reason for a bug bounty provider platform."""

    name: str
    available: bool
    reason: str


class StartProgramRequest(BaseModel):
    """Optional body for POST /api/providers/{name}/programs/{program_id}/start."""

    scope: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None


class ProviderUnavailableError(Exception):
    """Raised when an unavailable or unconfigured provider is queried."""


@runtime_checkable
class ProgramSource(Protocol):
    """Provider abstraction: one interface, N implementations."""

    name: str

    def available(self) -> bool:
        """Are credentials configured and platform available?"""
        ...

    def unavailable_reason(self) -> str:
        """Honest reason describing platform status or credential requirements."""
        ...

    def list_programs(self, refresh: bool = False) -> list[Program]:
        """Fetch and return the normalized program list."""
        ...

    def get_primary_scope(self, program_id: str) -> str:
        """Resolve the primary target hostname or scope for this program."""
        ...
