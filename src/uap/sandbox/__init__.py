"""Sandbox layer: in-process call-boundary guard (Master section 30).

Enforces filesystem/network/subprocess/timeout/output limits at the call
boundary. NOT an OS sandbox -- container isolation is section 30 phase 12.
"""

from __future__ import annotations

from uap.sandbox.executor import Sandbox, SandboxPolicy, SandboxViolation
from uap.sandbox.runner import TruncatingBuffer, run_code_in_sandbox

__all__ = [
    "SandboxPolicy",
    "SandboxViolation",
    "Sandbox",
    "TruncatingBuffer",
    "run_code_in_sandbox",
]
