from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

import uap.sandbox.executor as executor_mod
from uap.sandbox import Sandbox, SandboxPolicy, SandboxViolation


@pytest.mark.asyncio
async def test_memory_limit_exceeded() -> None:
    """1. A callable that allocates more than max_memory_mb is killed / returns a limit error."""
    # Modest limit of 64MB; attempt to allocate ~256MB
    sb = Sandbox(SandboxPolicy(max_memory_mb=64, max_cpu_seconds=5.0))

    def oom_callable() -> int:
        blob = bytearray(256 * 1024 * 1024)
        return len(blob)

    with pytest.raises(SandboxViolation) as exc_info:
        await sb.run_guarded(oom_callable)

    err_msg = str(exc_info.value).lower()
    assert "memory limit" in err_msg or "exit code" in err_msg or "terminated" in err_msg


@pytest.mark.asyncio
async def test_cpu_spin_terminated() -> None:
    """2. A callable that spins CPU longer than max_cpu_seconds is terminated."""
    # RLIMIT_CPU is integer-seconds; 1.0s provides an exact real OS CPU-seconds limit.
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=1.0))

    def cpu_spinner() -> int:
        s = 0
        while True:
            s += 1
        return s

    with pytest.raises(SandboxViolation) as exc_info:
        await sb.run_guarded(cpu_spinner)

    err_msg = str(exc_info.value).lower()
    assert "cpu limit" in err_msg or "timeout" in err_msg


@pytest.mark.asyncio
async def test_happy_path_normal_callable() -> None:
    """3. A normal callable returns its result unchanged (happy path)."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=2.0, max_output_bytes=1024))

    async def async_math(x: int, y: int) -> dict[str, int]:
        await asyncio.sleep(0.01)
        return {"sum": x + y, "product": x * y}

    result = await sb.run_guarded(async_math, 6, y=7)
    assert result == {"sum": 13, "product": 42}

    def sync_callable(text: str) -> str:
        return text.upper()

    sync_result = await sb.run_guarded(sync_callable, "hello world")
    assert sync_result == "HELLO WORLD"


@pytest.mark.asyncio
async def test_exception_propagation() -> None:
    """4. An exception inside the callable propagates as the documented error type."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=2.0))

    async def raise_value_error() -> None:
        await asyncio.sleep(0.01)
        raise ValueError("specific domain error value")

    with pytest.raises(ValueError, match="specific domain error value"):
        await sb.run_guarded(raise_value_error)

    def raise_key_error() -> None:
        raise KeyError("missing_item")

    with pytest.raises(KeyError, match="missing_item"):
        await sb.run_guarded(raise_key_error)


@pytest.mark.asyncio
async def test_unpicklable_return_value_policy() -> None:
    """5. A non-picklable return value is handled per documented policy."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=2.0))

    async def returns_unpicklable_lambda():
        return lambda x: x * 2

    with pytest.raises(SandboxViolation) as exc_info:
        await sb.run_guarded(returns_unpicklable_lambda)

    err_msg = str(exc_info.value).lower()
    assert "serialized" in err_msg or "pickl" in err_msg


def test_docstring_honesty() -> None:
    """6. The module docstring accurately lists enforced vs not-enforced."""
    doc = executor_mod.__doc__
    assert doc is not None
    assert "ENFORCED:" in doc
    assert "NOT ENFORCED:" in doc
    assert "max_memory_mb" in doc
    assert "max_cpu_seconds" in doc
    assert "RLIMIT_AS" in doc
    assert "RLIMIT_CPU" in doc
    assert "check_path" in doc
    assert "picklable" in doc


@pytest.mark.asyncio
async def test_subsecond_wall_clock_timeout() -> None:
    """Wall-clock timeout terminates slow tasks with tight timeouts."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=0.05))

    async def slow_task() -> str:
        await asyncio.sleep(0.5)
        return "done"

    with pytest.raises(SandboxViolation) as exc_info:
        await sb.run_guarded(slow_task)

    assert "timeout" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_output_truncation_across_fork() -> None:
    """Output bytes/str truncation applies across the process boundary."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=1.0, max_output_bytes=5))

    async def make_long_str() -> str:
        return "abcdefghijklmn"

    async def make_long_bytes() -> bytes:
        return b"1234567890"

    assert await sb.run_guarded(make_long_str) == "abcde"
    assert await sb.run_guarded(make_long_bytes) == b"12345"


@pytest.mark.asyncio
async def test_non_fork_fallback_degrades_honestly() -> None:
    """Degrade honestly when fork is unsupported: log warning and run in-process.

    Asserted in an isolated interpreter: the check patches a module attribute
    and reads that module's logger, both of which depend on which test modules
    already imported (or reloaded) the sandbox package. A fresh interpreter has
    no such history, so the result is deterministic.
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path as _Path

    script = textwrap.dedent(
        """
        import asyncio, logging
        from unittest.mock import patch

        from uap.sandbox import Sandbox, SandboxPolicy
        import uap.sandbox.executor as executor_module

        async def main():
            sb = Sandbox(SandboxPolicy(max_cpu_seconds=1.0))

            async def simple_callable() -> str:
                return "fallback_ok"

            records = []
            handler = logging.Handler()
            handler.emit = records.append
            logger = executor_module.logger
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            try:
                with patch.object(executor_module, "_can_fork", return_value=False):
                    result = await sb.run_guarded(simple_callable)
            finally:
                logger.removeHandler(handler)

            assert result == "fallback_ok", result
            messages = [r.getMessage() for r in records]
            assert any("not supported on this platform" in m for m in messages), messages
            print("ISOLATED SANDBOX FALLBACK OK")

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_Path(__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, (
        f"isolated sandbox check failed\n--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )

@pytest.mark.asyncio
async def test_unpicklable_exception_handling() -> None:
    """An unpicklable exception inside callable raises RuntimeError with name and message."""
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=2.0))

    def raise_local_exception():
        class UnpicklableLocalError(Exception):
            pass

        raise UnpicklableLocalError("cannot be pickled")

    with pytest.raises(RuntimeError) as exc_info:
        await sb.run_guarded(raise_local_exception)

    assert "UnpicklableLocalError" in str(exc_info.value)
    assert "cannot be pickled" in str(exc_info.value)
