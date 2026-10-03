"""Sandbox execution entry point (Master section 30).

Provides guarded execution for code snippets submitted via the API or UI editor,
enforcing CPU, memory, timeout, and output constraints via :class:`uap.sandbox.Sandbox`.
"""

from __future__ import annotations

import io
import logging
import sys
import time
import traceback
from typing import Any

from uap.sandbox.executor import Sandbox, SandboxPolicy, SandboxViolation

logger = logging.getLogger(__name__)

__all__ = ["TruncatingBuffer", "run_code_in_sandbox"]


class TruncatingBuffer(io.TextIOBase):
    """An in-memory text buffer that truncates when encoded bytes reach ``max_bytes``."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max(0, int(max_bytes))
        self.bytes_written = 0
        self.truncated = False
        self._parts: list[str] = []

    def write(self, s: str) -> int:
        if not s:
            return 0
        encoded = s.encode("utf-8", errors="replace")
        n_bytes = len(encoded)
        if self.bytes_written + n_bytes > self.max_bytes:
            self.truncated = True
            remaining = max(0, self.max_bytes - self.bytes_written)
            if remaining > 0:
                self._parts.append(encoded[:remaining].decode("utf-8", errors="ignore"))
                self.bytes_written += remaining
            return len(s)
        self._parts.append(s)
        self.bytes_written += n_bytes
        return len(s)

    def flush(self) -> None:
        pass

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def getvalue(self) -> str:
        return "".join(self._parts)


def _child_exec_python(code: str, max_output_bytes: int) -> dict[str, Any]:
    """Execute Python code in the forked child process with redirected streams."""
    out_buf = TruncatingBuffer(max_output_bytes)
    err_buf = TruncatingBuffer(max_output_bytes)

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    sys.stdout = out_buf
    sys.stderr = err_buf

    exit_code = 0
    try:
        compiled = compile(code, "<string>", "exec")
        namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__doc__": None,
            "__package__": None,
        }
        exec(compiled, namespace)
    except SystemExit as exc:
        if exc.code is None:
            exit_code = 0
        elif isinstance(exc.code, int):
            exit_code = exc.code
        else:
            sys.stderr.write(f"{exc.code}\n")
            exit_code = 1
    except MemoryError:
        # Re-raise MemoryError so executor._child_runner and Sandbox.run_guarded
        # handle it as an RLIMIT_AS memory limit violation!
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        exit_code = 1
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr

    return {
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "exit_code": exit_code,
        "truncated": out_buf.truncated or err_buf.truncated,
    }


async def run_code_in_sandbox(
    *,
    code: str,
    language: str = "python",
    timeout_s: float = 5.0,
    max_memory_mb: int = 512,
    max_output_bytes: int = 65536,
) -> dict[str, Any]:
    """Run code under sandbox resource limits and return a structured execution result.

    Enforces RLIMIT_AS (memory limit), RLIMIT_CPU / wall-clock timeout, and output
    truncation via :class:`uap.sandbox.Sandbox`.
    """
    if language != "python":
        raise ValueError(
            f"Unsupported language '{language}'. Supported languages: python"
        )

    policy = SandboxPolicy(
        max_cpu_seconds=timeout_s,
        max_memory_mb=max_memory_mb,
        max_output_bytes=max_output_bytes,
    )
    sandbox = Sandbox(policy)

    t0 = time.perf_counter()
    try:
        raw_result = await sandbox.run_guarded(
            _child_exec_python, code, max_output_bytes
        )
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        res = dict(raw_result)
        res["duration_ms"] = duration_ms

        if res.get("truncated"):
            trunc_msg = f"[output truncated: exceeded limit of {max_output_bytes} bytes]"
            current_stderr = res.get("stderr", "")
            if current_stderr:
                res["stderr"] = f"{current_stderr}\n{trunc_msg}"
            else:
                res["stderr"] = trunc_msg
        return res
    except SandboxViolation as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        err_msg = str(exc)
        err_lower = err_msg.lower()

        if "cpu limit" in err_lower or "sigxcpu" in err_lower:
            exit_code = 152
        elif "timeout" in err_lower:
            exit_code = 124
        elif "memory" in err_lower or "sigkill" in err_lower:
            exit_code = 137
        else:
            exit_code = 1

        return {
            "stdout": "",
            "stderr": f"Sandbox limit exceeded: {err_msg}",
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "truncated": False,
        }
