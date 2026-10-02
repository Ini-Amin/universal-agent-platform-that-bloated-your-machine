"""In-process sandbox guard (Master section 30).

Enforces capability-shaped limits -- filesystem paths, network hosts,
subprocess use, output size, and a wall-clock timeout -- at the *call
boundary*. :meth:`Sandbox.run_guarded` wraps an async callable with
``asyncio.wait_for`` and truncates its output.

HONEST LIMITATION: this is an in-process guard, NOT an OS sandbox. It cannot
stop a function that bypasses :meth:`check_path` / :meth:`check_network` and
touches the filesystem or network directly, and the timeout cannot interrupt
blocking (non-``await``) CPU work -- ``wait_for`` only cancels at ``await``
points. ``max_memory_mb`` / ``max_cpu_seconds`` are declared limits a real OS
sandbox (container isolation, section 30 phase 12) must enforce; here only the
timeout and output size are actually imposed. Security is represented by
explicit capability + policy enforcement (sections 29, 30, 31), not by this
guard alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SandboxPolicy", "SandboxViolation", "Sandbox"]


class SandboxViolation(Exception):
    """Raised when guarded code requests a resource the policy forbids."""


class SandboxPolicy(BaseModel):
    """Declarative limits for a guarded call. Fail-closed defaults."""

    model_config = ConfigDict(extra="forbid")

    allow_fs_write_paths: tuple[str, ...] = ()
    allow_fs_read_paths: tuple[str, ...] = ()
    allow_network: bool = False
    allow_network_hosts: tuple[str, ...] = ()
    allow_any_host: bool = False
    max_cpu_seconds: float = 5.0
    max_memory_mb: int = 512
    max_output_bytes: int = 65536
    allow_subprocess: bool = False


class Sandbox:
    """Enforces a :class:`SandboxPolicy` at the call boundary."""

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    # ------------------------------------------------------------------ #
    # Resource checks (callers invoke these before touching resources)
    # ------------------------------------------------------------------ #

    def check_path(self, path: str | Path, write: bool) -> Path:
        """Allow a filesystem access only under an allowed prefix. Else raise."""
        resolved = Path(path).resolve()
        allowed = (
            self._policy.allow_fs_write_paths
            if write
            else self._policy.allow_fs_read_paths
        )
        for prefix in allowed:
            root = Path(prefix).resolve()
            if resolved == root or root in resolved.parents:
                return resolved
        verb = "write" if write else "read"
        raise SandboxViolation(f"fs {verb} denied outside allowed prefixes: {resolved}")

    def check_network(self, host: str) -> str:
        """Allow a host only when networking is enabled and the host is listed.

        Fail-closed default: ``allow_network=True`` with an empty
        ``allow_network_hosts`` denies ALL hosts unless ``allow_any_host=True``
        is explicitly set as an escape hatch.
        """
        if not self._policy.allow_network:
            raise SandboxViolation("network access disabled")
        if self._policy.allow_any_host:
            return host
        hosts = self._policy.allow_network_hosts
        if not hosts:
            raise SandboxViolation(
                f"network host denied (empty allowlist; allow_any_host=False): {host}"
            )
        if host not in hosts:
            raise SandboxViolation(f"network host not allowed: {host}")
        return host
    def check_subprocess(self) -> None:
        """Allow subprocess use only when the policy opts in. Else raise."""
        if not self._policy.allow_subprocess:
            raise SandboxViolation("subprocess execution disabled")

    # ------------------------------------------------------------------ #
    # Output / execution
    # ------------------------------------------------------------------ #

    def truncate_output(self, data: str | bytes) -> str | bytes:
        """Clamp output to ``max_output_bytes`` (counting raw bytes)."""
        limit = self._policy.max_output_bytes
        if isinstance(data, str):
            encoded = data.encode("utf-8")
            if len(encoded) <= limit:
                return data
            return encoded[:limit].decode("utf-8", errors="ignore")
        if len(data) <= limit:
            return data
        return data[:limit]

    async def run_guarded(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn`` under the timeout; truncate output; re-raise its errors.

        A timeout maps to :class:`SandboxViolation`. Other exceptions from
        ``fn`` propagate unchanged.
        """
        try:
            result = await asyncio.wait_for(
                fn(*args, **kwargs), timeout=self._policy.max_cpu_seconds
            )
        except asyncio.TimeoutError as exc:
            raise SandboxViolation(
                f"execution exceeded {self._policy.max_cpu_seconds}s timeout"
            ) from exc
        if isinstance(result, (str, bytes)):
            return self.truncate_output(result)
        return result
