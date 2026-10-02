"""Implementation-agnostic tool surface (Master sections 12 and 13).

Agents ask for capabilities by name and never care whether the callable behind
them is a local Python function, a REST API client, or an MCP tool. The registry
stores only (ToolSpec, callable); an MCP adapter is a later build step that
registers tools exactly like `local.py` does.

Risk tier vocabulary (docs/research/bugbounty-mcp-inventory.md section 4):
0 pure local, 1 passive external, 2 active external, 3 side-effectful.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import ApprovalRequest, ToolCall, ToolResult
from uap.credentials import (
    CredentialExpiredError,
    CredentialNotFoundError,
    CredentialRevokedError,
)
from uap.sandbox import SandboxViolation

if TYPE_CHECKING:
    from uap.approval.gate import ApprovalGate
    from uap.capability.resolver import CapabilityResolver
    from uap.credentials.manager import CredentialManager
    from uap.observability.events import EventBus
    from uap.policy.engine import PolicyEngine
    from uap.sandbox.executor import Sandbox
    from uap.tools.policy import ToolPolicy
RISK_TIERS: dict[int, str] = {
    0: "pure local",
    1: "passive external",
    2: "active external",
    3: "side-effectful",
}

ToolFn = Callable[..., Awaitable[Any]]


class ToolSpec(BaseModel):
    """What an agent sees of a tool: name, purpose, risk, and argument shape."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    risk_tier: int = Field(ge=0, le=3)
    input_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required_credentials: list[str] = Field(default_factory=list)
    sandboxed: bool = False
    @property
    def risk_label(self) -> str:
        return RISK_TIERS[self.risk_tier]


class ToolRegistry:
    """Named tools plus the deterministic guard that gates every call."""

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        *,
        policy_engine: PolicyEngine | None = None,
        capability_resolver: CapabilityResolver | None = None,
        approval_gate: ApprovalGate | None = None,
        event_bus: EventBus | None = None,
        subject: str = "agent",
        credential_manager: CredentialManager | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolFn]] = {}
        if policy is None:
            # Deferred: policy imports this module for ToolSpec/RISK_TIERS.
            from uap.tools.policy import ToolPolicy

            policy = ToolPolicy()
        self.policy = policy
        self.policy_engine = policy_engine
        self.capability_resolver = capability_resolver
        self.approval_gate = approval_gate
        self.event_bus = event_bus
        self.subject = subject
        self.credential_manager = credential_manager
        self.sandbox = sandbox
        # ponytail: unbounded list is fine for a single task; swap for a bounded
        # ring or an observer when wiring observability to long-running sessions.
        self.calls: list[ToolCall] = []

    def register(self, spec: ToolSpec, fn: ToolFn) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = (spec, fn)

    def get(self, name: str) -> tuple[ToolSpec, ToolFn] | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    async def call(
        self,
        name: str,
        args: dict,
        *,
        approval: ApprovalRequest | None = None,
        task_id: str | None = None,
        subject: str | None = None,
        approval_gate: ApprovalGate | None = None,
        event_bus: Any | None = None,
    ) -> ToolResult:
        recorded_args = dict(args)
        if self.credential_manager is not None:
            for k, v in recorded_args.items():
                if isinstance(v, str):
                    recorded_args[k] = self.credential_manager.redact(v)

        call = ToolCall(tool=name, args=recorded_args)
        self.calls.append(call)
        started = time.perf_counter()

        entry = self._tools.get(name)
        if entry is None:
            return self._finish(call, started, ok=False, error=f"unknown tool: {name}")

        spec, fn = entry
        decision = self.policy.check(spec, approval)
        if not decision.allowed:
            gate = approval_gate or self.approval_gate
            bus = event_bus or self.event_bus
            created_approval_id: str | None = None
            if decision.needs_approval and gate is not None and approval is None:
                eff_task_id = task_id or "default"
                req = gate.request(
                    task_id=eff_task_id,
                    action=spec.name,
                    details={"args": dict(args), "tier": spec.risk_tier},
                )
                created_approval_id = req.approval_id
                if bus is not None:
                    from uap.observability.events import EventKind

                    bus.emit_kind(
                        EventKind.APPROVAL,
                        task_id=eff_task_id,
                        data={
                            "approval_id": req.approval_id,
                            "action": spec.name,
                            "tier": spec.risk_tier,
                            "args": dict(args),
                        },
                    )
            return self._finish(
                call,
                started,
                ok=False,
                error=decision.reason,
                approval_id=created_approval_id,
            )

        if self.policy_engine is not None:
            from uap.policy.engine import PolicyEffect

            subj = subject or self.subject
            resource = str(
                args.get("path")
                or args.get("target")
                or args.get("url")
                or args.get("resource")
                or spec.name
            )
            effect, reason = self.policy_engine.decide(
                subj, spec.name, resource, context=args
            )
            if effect != PolicyEffect.DENY and not spec.name.startswith("tool:"):
                tool_effect, tool_reason = self.policy_engine.decide(
                    subj, f"tool:{spec.name}", resource, context=args
                )
                if tool_effect == PolicyEffect.DENY:
                    effect, reason = tool_effect, tool_reason
                elif effect == PolicyEffect.ASK and tool_effect == PolicyEffect.ALLOW:
                    effect, reason = tool_effect, tool_reason

            if effect == PolicyEffect.DENY:
                return self._finish(call, started, ok=False, error=reason)

        if self.capability_resolver is not None:
            subj = subject or self.subject
            scope = (
                args.get("scope")
                or args.get("target")
                or args.get("domain")
                or args.get("host")
                or args.get("path")
            )
            if scope is not None:
                scope = str(scope)
            allowed, reason = self._check_capability(spec, subj, scope=scope)
            if not allowed:
                return self._finish(call, started, ok=False, error=reason)

        for field, meta in spec.input_schema.items():
            if (
                meta.get("required")
                and field not in args
                and field not in spec.required_credentials
            ):
                return self._finish(
                    call, started, ok=False, error=f"missing required arg: {field}"
                )

        # -------------------------------------------------------------- #
        # Credential resolution (Master section 32: runtime injection)
        # -------------------------------------------------------------- #
        resolved_credentials: dict[str, str] = {}
        if spec.required_credentials:
            if self.credential_manager is None:
                return self._finish(
                    call,
                    started,
                    ok=False,
                    error=f"missing required credential: {spec.required_credentials[0]}",
                )
            for cred_key in spec.required_credentials:
                try:
                    val = self.credential_manager.resolve(cred_key)
                    resolved_credentials[cred_key] = val
                    ref = self.credential_manager.get_ref(cred_key)
                    if ref is not None and ref.name != cred_key:
                        resolved_credentials[ref.name] = val
                except CredentialRevokedError:
                    return self._finish(
                        call,
                        started,
                        ok=False,
                        error=f"credential revoked: {cred_key}",
                    )
                except CredentialExpiredError:
                    return self._finish(
                        call,
                        started,
                        ok=False,
                        error=f"credential expired: {cred_key}",
                    )
                except (CredentialNotFoundError, KeyError):
                    return self._finish(
                        call,
                        started,
                        ok=False,
                        error=f"missing required credential: {cred_key}",
                    )
                except Exception:
                    return self._finish(
                        call,
                        started,
                        ok=False,
                        error=f"failed to resolve credential: {cred_key}",
                    )

        # -------------------------------------------------------------- #
        # Execution (under Sandbox if sandboxed=True)
        # -------------------------------------------------------------- #
        fn_kwargs = dict(args)
        fn_kwargs.update(resolved_credentials)

        try:
            if spec.sandboxed:
                if self.sandbox is None:
                    return self._finish(
                        call,
                        started,
                        ok=False,
                        error=f"tool '{spec.name}' requires sandbox but no sandbox configured",
                    )
                raw_result = await self.sandbox.run_guarded(fn, **fn_kwargs)
            else:
                raw_result = await fn(**fn_kwargs)
            return self._finish(call, started, ok=True, result=raw_result)
        except Exception as exc:  # a tool failure is data, never a raised error
            return self._finish(
                call, started, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
    def _check_capability(
        self, spec: ToolSpec, subject: str, scope: str | None = None
    ) -> tuple[bool, str]:
        if self.capability_resolver is None:
            return True, "allowed"

        if spec.risk_tier >= 2:
            primary = "network"
            candidates = ["network", spec.name]
        elif spec.risk_tier == 1:
            primary = "network"
            candidates = ["network", "passive-network", spec.name]
        else:
            primary = "local-only"
            candidates = ["local-only", "local", spec.name]

        allowed, reason = self.capability_resolver.check(subject, primary, scope=scope)
        if allowed:
            return True, reason

        for alt in candidates[1:]:
            alt_allowed, alt_reason = self.capability_resolver.check(
                subject, alt, scope=scope
            )
            if alt_allowed:
                return True, alt_reason

        return False, reason

    def _finish(
        self,
        call: ToolCall,
        started: float,
        *,
        ok: bool,
        error: str | None = None,
        result: Any = None,
        approval_id: str | None = None,
    ) -> ToolResult:
        if error is not None and self.credential_manager is not None:
            error = self.credential_manager.redact(error)
        if isinstance(result, str) and self.credential_manager is not None:
            result = self.credential_manager.redact(result)
        return ToolResult(
            call_id=call.call_id,
            tool=call.tool,
            ok=ok,
            result=result,
            error=error,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            approval_id=approval_id,
        )
