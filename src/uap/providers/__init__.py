"""Program discovery layer for bug bounty platforms."""

from __future__ import annotations

from uap.providers.base import (
    Program,
    ProgramSource,
    ProviderStatus,
    ProviderUnavailableError,
    StartProgramRequest,
)
from uap.providers.hackerone import HackerOneSource
from uap.providers.immunefi import ImmunefiSource
from uap.providers.registry import ProviderRegistry, get_default_registry
from uap.providers.yeswehack import YesWeHackSource

__all__ = [
    "Program",
    "ProgramSource",
    "ProviderStatus",
    "ProviderUnavailableError",
    "StartProgramRequest",
    "get_default_registry",
    "YesWeHackSource",
    "HackerOneSource",
    "ImmunefiSource",
]
