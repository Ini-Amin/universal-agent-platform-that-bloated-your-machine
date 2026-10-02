"""In-process and child-process sandbox guard (Master section 30).

Enforces capability-shaped limits at the call boundary and executes guarded
callables in an isolated forked child process when supported.

ENFORCED:
- Memory limit (:attr:`SandboxPolicy.max_memory_mb`): Enforced in a forked child
  process via ``resource.setrlimit(resource.RLIMIT_AS, ...)`` on platforms
  supporting fork and rlimit (e.g. Linux). Exceeding this limit triggers a
  :class:`SandboxViolation`.
- CPU limit (:attr:`SandboxPolicy.max_cpu_seconds`): Enforced at the OS level
  via ``resource.RLIMIT_CPU`` in the child process (sending SIGXCPU on expiry).
- Wall-clock timeout (:attr:`SandboxPolicy.max_cpu_seconds`): Enforced by the
  parent process awaiting child completion; kills child process on timeout.
- Output truncation (:attr:`SandboxPolicy.max_output_bytes`): Truncates return
  values of type ``str`` or ``bytes`` to the byte limit.
- Call-boundary checks: Cooperative :meth:`check_path`, :meth:`check_network`,
  and :meth:`check_subprocess` calls before resource access.

NOT ENFORCED:
- Direct syscall / uncooperative I/O interception: Functions that bypass
  :meth:`check_path` / :meth:`check_network` can still access host filesystem
  and network directly (requires OS containers / seccomp / mount namespaces).
- Sub-second CPU limit granularity: POSIX ``RLIMIT_CPU`` operates in whole
  seconds; sub-second limits rely on the wall-clock timeout line of defence.
- Non-picklable results: Return values must be picklable to cross the process
  pipe boundary; unpicklable return values raise :class:`SandboxViolation`.
- Non-fork platforms: Systems without ``fork`` or ``resource`` support degrade
  to in-process execution without memory or OS CPU limiting (logged warning).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import multiprocessing as mp
import signal
import warnings
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = ["SandboxPolicy", "SandboxViolation", "Sandbox"]


def _can_fork() -> bool:
    # ponytail: Linux fork+rlimit is primary; Windows/Wasm upgrade path requires OS container shim.
    return (
        hasattr(mp, "get_context")
        and "fork" in mp.get_all_start_methods()
        and resource is not None
    )


def _child_runner(
    conn: Any,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    max_memory_mb: int,
    max_cpu_seconds: float,
) -> None:
    try:
        if resource is not None:
            if max_memory_mb > 0 and hasattr(resource, "RLIMIT_AS"):
                limit_bytes = int(max_memory_mb) * 1024 * 1024
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
                except (ValueError, OSError):
                    pass
            if max_cpu_seconds > 0 and hasattr(resource, "RLIMIT_CPU"):
                cpu_sec = max(1, math.ceil(max_cpu_seconds))
                try:
                    resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec + 1))
                except (ValueError, OSError):
                    pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                res = loop.run_until_complete(res)
        finally:
            loop.close()

        fn_state = None
        target_obj = getattr(fn, "__self__", fn)
        if hasattr(target_obj, "__dict__"):
            try:
                fn_state = dict(target_obj.__dict__)
            except Exception:
                fn_state = None

        try:
            conn.send(("ok", res, fn_state))
        except Exception:
            try:
                conn.send(("ok", res, None))
            except Exception as exc:
                conn.send(("unpicklable_result", type(exc).__name__, str(exc)))
    except MemoryError as exc:
        try:
            conn.send(("memory_limit", str(exc)))
        except Exception:
            pass
    except BaseException as exc:
        try:
            conn.send(("exception", exc))
        except Exception:
            try:
                conn.send(("unpicklable_exception", type(exc).__name__, str(exc)))
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
        """Run ``fn`` under sandbox limits; truncate output; re-raise errors.

        Executes ``fn`` in a forked child process enforcing memory and CPU
        resource limits where available. A timeout or resource violation maps to
        :class:`SandboxViolation`. Normal exceptions from ``fn`` propagate unchanged.
        """
        if not _can_fork():
            logger.warning(
                "Fork or resource limit not supported on this platform; "
                "falling back to in-process execution without memory/CPU rlimit enforcement."
            )
            try:
                target = fn(*args, **kwargs)
                if inspect.isawaitable(target):
                    result = await asyncio.wait_for(
                        target, timeout=self._policy.max_cpu_seconds
                    )
                else:
                    result = target
            except asyncio.TimeoutError as exc:
                raise SandboxViolation(
                    f"execution exceeded {self._policy.max_cpu_seconds}s timeout"
                ) from exc
            if isinstance(result, (str, bytes)):
                return self.truncate_output(result)
            return result

        ctx = mp.get_context("fork")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        p = ctx.Process(
            target=_child_runner,
            args=(
                child_conn,
                fn,
                args,
                kwargs,
                self._policy.max_memory_mb,
                self._policy.max_cpu_seconds,
            ),
        )
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=DeprecationWarning,
                message=".*multi-threaded.*use of fork.*",
            )
            p.start()
        child_conn.close()

        def _read_from_child() -> tuple[str, Any]:
            try:
                return ("data", parent_conn.recv())
            except EOFError:
                p.join()
                return ("eof", p.exitcode)
            finally:
                p.join()

        try:
            read_task = asyncio.create_task(asyncio.to_thread(_read_from_child))
            status, payload = await asyncio.wait_for(
                asyncio.shield(read_task),
                timeout=self._policy.max_cpu_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            p.join(timeout=0.02)
            sigxcpu = getattr(signal, "SIGXCPU", None)
            if sigxcpu is not None and p.exitcode == -sigxcpu:
                if p.is_alive():
                    p.kill()
                try:
                    await read_task
                except Exception:
                    pass
                raise SandboxViolation(
                    f"execution exceeded {self._policy.max_cpu_seconds}s CPU limit (SIGXCPU)"
                ) from exc
            if p.is_alive():
                p.kill()
            try:
                await read_task
            except Exception:
                pass
            raise SandboxViolation(
                f"execution exceeded {self._policy.max_cpu_seconds}s timeout"
            ) from exc
        finally:
            if p.is_alive():
                p.kill()
                p.join()
            parent_conn.close()

        if status == "data":
            tag = payload[0]
            if tag == "ok":
                result = payload[1]
                fn_state = payload[2] if len(payload) > 2 else None
                if fn_state is not None:
                    target_obj = getattr(fn, "__self__", fn)
                    if hasattr(target_obj, "__dict__"):
                        try:
                            target_obj.__dict__.update(fn_state)
                        except Exception:
                            pass
                if isinstance(result, (str, bytes)):
                    return self.truncate_output(result)
                return result
            if tag == "memory_limit":
                raise SandboxViolation(
                    f"execution exceeded {self._policy.max_memory_mb}MB memory limit"
                )
            if tag == "exception":
                raise payload[1]
            if tag == "unpicklable_result":
                raise SandboxViolation(
                    f"guarded return value could not be serialized across process boundary: {payload[1]}: {payload[2]}"
                )
            if tag == "unpicklable_exception":
                raise RuntimeError(
                    f"guarded execution raised unpicklable error {payload[1]}: {payload[2]}"
                )
            raise SandboxViolation(f"unexpected child worker message: {tag}")

        sigxcpu = getattr(signal, "SIGXCPU", None)
        if sigxcpu is not None and payload == -sigxcpu:
            raise SandboxViolation(
                f"execution exceeded {self._policy.max_cpu_seconds}s CPU limit (SIGXCPU)"
            )
        if payload == -signal.SIGKILL:
            raise SandboxViolation(
                "execution terminated by signal SIGKILL (memory limit or external termination)"
            )
        if payload == 0:
            raise SandboxViolation("child process exited without returning a result")
        raise SandboxViolation(
            f"child process terminated unexpectedly with exit code {payload}"
        )
