"""Tests for runtime credential injection and tool sandbox integration (Master sections 22, 24, 30, 32).

Verifies:
1. Tool with a declared credential gets it injected at call time (spy captures value).
2. The secret NEVER appears in ToolCall.args, ToolResult, call log, or error strings.
3. Missing credential -> failed result with non-secret reason; fn never ran.
4. Expired/revoked credential -> failed result with non-secret reason; fn never ran.
5. Sandboxed tool: honest integration (timeout, output truncation, fail-closed when unconfigured).
6. Unconfigured registry: identical behavior to today (regression guard).
7. Credential values are not serialized into registry.calls history.
8. Ordering enforced: policy -> capability -> credential -> sandbox -> fn.
9. Credential resolution works by either name or ref_id.
10. Documented in-process limitations of sandbox.
"""

from __future__ import annotations

import asyncio
import json
from uap.contracts import ApprovalRequest, ApprovalState, ToolCall, ToolResult
from typing import Any

import pytest

from uap.capability import CapabilityResolver
from uap.contracts import ApprovalRequest, ToolCall, ToolResult
from uap.credentials import (
    CredentialExpiredError,
    CredentialManager,
    CredentialNotFoundError,
    CredentialRef,
    CredentialRevokedError,
)
from uap.policy import PolicyEffect, PolicyEngine, PolicyRule
from uap.sandbox import Sandbox, SandboxPolicy, SandboxViolation
from uap.tools import ToolPolicy, ToolRegistry, ToolSpec, register_local_tools


class SpyTool:
    """Spy callable to inspect received kwargs and return a controlled result."""

    def __init__(self, return_value: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.return_value = return_value or {"status": "ok"}

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self.return_value

    @property
    def called(self) -> bool:
        return len(self.calls) > 0


# --------------------------------------------------------------------------- #
# Test 1: Credential injection at call time
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_credential_injected_at_call_time(tmp_path: Path) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    secret_value = "SECRET_TOKEN_ABCD_1234"
    mgr.register("github_token", secret_value, kind="token")

    spy = SpyTool({"data": "repos"})
    spec = ToolSpec(
        name="github_list_repos",
        description="List repos for org",
        risk_tier=0,
        required_credentials=["github_token"],
    )

    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec, spy)

    result = await registry.call("github_list_repos", {"org": "acme"})

    assert result.ok is True
    assert result.result == {"data": "repos"}
    assert spy.called is True
    assert len(spy.calls) == 1
    # Spy captured the injected credential
    assert spy.calls[0]["org"] == "acme"
    assert spy.calls[0]["github_token"] == secret_value


# --------------------------------------------------------------------------- #
# Test 2: Secret NEVER appears in ToolCall.args, ToolResult, call log, or errors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_secret_never_appears_in_call_args_result_log_or_errors(
    tmp_path: Path,
) -> None:
    secret_value = "ULTRA_CONFIDENTIAL_KEY_9999"
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    mgr.register("api_key", secret_value, kind="token")

    async def leaky_tool(arg: str, api_key: str) -> dict[str, str]:
        # Raises an exception that includes the secret value
        raise ValueError(f"Provider failed with secret value {api_key}")

    spec = ToolSpec(
        name="leaky_tool",
        description="Tool that raises error with secret",
        risk_tier=0,
        required_credentials=["api_key"],
    )

    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec, leaky_tool)

    result = await registry.call("leaky_tool", {"arg": "test_input"})

    assert result.ok is False
    # 1. Error string has secret redacted
    assert secret_value not in result.error
    assert "[REDACTED:api_key]" in result.error

    # 2. Entire ToolResult serialized dump does NOT contain secret
    result_dump = result.model_dump_json()
    assert secret_value not in result_dump

    # 3. ToolCall.args does NOT contain secret
    assert len(registry.calls) == 1
    recorded_call = registry.calls[0]
    assert secret_value not in json.dumps(recorded_call.args)
    assert secret_value not in recorded_call.model_dump_json()

    # 4. Entire calls log dump does NOT contain secret
    calls_dump = json.dumps([c.model_dump(mode="json") for c in registry.calls])
    assert secret_value not in calls_dump


# --------------------------------------------------------------------------- #
# Test 3: Missing credential -> failed result with non-secret reason; fn not run
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_credential_fails_with_non_secret_reason(
    tmp_path: Path,
) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    spy = SpyTool()
    spec = ToolSpec(
        name="secure_tool",
        description="Requires missing credential",
        risk_tier=0,
        required_credentials=["nonexistent_token"],
    )

    # 3a: Credential manager present, but credential not found
    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec, spy)

    result = await registry.call("secure_tool", {"target": "example.com"})

    assert result.ok is False
    assert "missing required credential: nonexistent_token" in result.error
    assert not spy.called

    # 3b: No credential manager configured on registry
    spy_unconfigured = SpyTool()
    unconfigured_registry = ToolRegistry()
    unconfigured_registry.register(spec, spy_unconfigured)

    result_unconf = await unconfigured_registry.call(
        "secure_tool", {"target": "example.com"}
    )
    assert result_unconf.ok is False
    assert "missing required credential: nonexistent_token" in result_unconf.error
    assert not spy_unconfigured.called


# --------------------------------------------------------------------------- #
# Test 4: Expired/revoked credential -> failed result with non-secret reason
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_and_revoked_credentials_fail(tmp_path: Path) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    revoked_secret = "SECRET_REVOKED_VALUE_555"
    expired_secret = "SECRET_EXPIRED_VALUE_777"

    mgr.register("revoked_key", revoked_secret, kind="token")
    mgr.revoke("revoked_key")

    mgr.register("expired_key", expired_secret, kind="token")
    mgr.expire("expired_key")

    spy_revoked = SpyTool()
    spec_revoked = ToolSpec(
        name="revoked_tool",
        description="Uses revoked cred",
        risk_tier=0,
        required_credentials=["revoked_key"],
    )

    spy_expired = SpyTool()
    spec_expired = ToolSpec(
        name="expired_tool",
        description="Uses expired cred",
        risk_tier=0,
        required_credentials=["expired_key"],
    )

    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec_revoked, spy_revoked)
    registry.register(spec_expired, spy_expired)

    # Revoked call
    res_revoked = await registry.call("revoked_tool", {})
    assert res_revoked.ok is False
    assert "credential revoked: revoked_key" in res_revoked.error
    assert revoked_secret not in res_revoked.error
    assert not spy_revoked.called

    # Expired call
    res_expired = await registry.call("expired_tool", {})
    assert res_expired.ok is False
    assert "credential expired: expired_key" in res_expired.error
    assert expired_secret not in res_expired.error
    assert not spy_expired.called


# --------------------------------------------------------------------------- #
# Test 5: Sandboxed tool honest integration (timeout, output clamp, fail-closed)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sandboxed_tool_honest_integration() -> None:
    # Configure sandbox with strict limits
    policy = SandboxPolicy(max_cpu_seconds=0.05, max_output_bytes=12)
    sandbox = Sandbox(policy)
    registry = ToolRegistry(sandbox=sandbox)

    # 5a: Normal execution under sandbox
    async def ok_fn() -> str:
        return "hello world"

    spec_ok = ToolSpec(
        name="ok_tool",
        description="Fast tool",
        risk_tier=0,
        sandboxed=True,
    )
    registry.register(spec_ok, ok_fn)
    res_ok = await registry.call("ok_tool", {})
    assert res_ok.ok is True
    assert res_ok.result == "hello world"

    # 5b: Output truncation imposed by Sandbox
    async def verbose_fn() -> str:
        return "1234567890EXTRA_OUTPUT_TO_TRUNCATE"

    spec_verbose = ToolSpec(
        name="verbose_tool",
        description="Tool producing large output",
        risk_tier=0,
        sandboxed=True,
    )
    registry.register(spec_verbose, verbose_fn)
    res_verbose = await registry.call("verbose_tool", {})
    assert res_verbose.ok is True
    assert res_verbose.result == "1234567890EX"  # Clamped to 12 bytes

    # 5c: Timeout violation imposed by Sandbox
    async def slow_fn() -> str:
        await asyncio.sleep(0.2)
        return "finished"

    spec_slow = ToolSpec(
        name="slow_tool",
        description="Tool that takes too long",
        risk_tier=0,
        sandboxed=True,
    )
    registry.register(spec_slow, slow_fn)
    res_slow = await registry.call("slow_tool", {})
    assert res_slow.ok is False
    assert "SandboxViolation" in res_slow.error
    assert "timeout" in res_slow.error

    # 5d: Tool requires sandbox but registry has none configured -> fail closed
    unconfigured_reg = ToolRegistry()
    unconfigured_reg.register(spec_ok, ok_fn)
    res_fail_closed = await unconfigured_reg.call("ok_tool", {})
    assert res_fail_closed.ok is False
    assert "requires sandbox but no sandbox configured" in res_fail_closed.error


# --------------------------------------------------------------------------- #
# Test 5b: Sandboxed tool documented in-process non-support
# --------------------------------------------------------------------------- #


def test_sandbox_documented_inprocess_limitations() -> None:
    """Documents honest non-support: in-process guard cannot isolate OS resources.

    As documented in uap.sandbox.executor:
    - No container/process isolation (section 30 phase 12)
    - max_memory_mb is declared but not enforced in-process
    - Direct filesystem/network access cannot be intercepted without cooperative check_* calls
    """
    sb = Sandbox(SandboxPolicy(max_memory_mb=128, max_cpu_seconds=1.0))
    # Declarative limits exist on the policy
    assert sb.policy.max_memory_mb == 128
    # But Sandbox.run_guarded only imposes wait_for and output clamp
    assert hasattr(sb, "run_guarded")
    assert not hasattr(sb, "enforce_memory_limit")


# --------------------------------------------------------------------------- #
# Test 6: Unconfigured registry behavior identical to today (regression guard)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unconfigured_registry_identical_behavior(tmp_path: Path) -> None:
    registry = ToolRegistry()

    assert registry.credential_manager is None
    assert registry.sandbox is None
    assert registry.policy_engine is None
    assert registry.capability_resolver is None

    register_local_tools(registry, tmp_path)

    # Echo works identically
    res_echo = await registry.call("echo", {"msg": "hello", "count": 42})
    assert res_echo.ok is True
    assert res_echo.result == {"msg": "hello", "count": 42}
    # Write (tier 3) requires approval; read (tier 0) does not
    approval = ApprovalRequest(
        task_id="task_1",
        action="write_artifact_file",
        state=ApprovalState.APPROVED,
        decided_by="tester",
    )
    res_write = await registry.call(
        "write_artifact_file",
        {"path": "test.txt", "content": "hello file"},
        approval=approval,
    )
    assert res_write.ok is True

    res_read = await registry.call("read_text_file", {"path": "test.txt"})
    assert res_read.ok is True
    assert res_read.result == "hello file"


# --------------------------------------------------------------------------- #
# Test 7: Credential values are not serialized into registry.calls history
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_credentials_not_serialized_into_calls_history(
    tmp_path: Path,
) -> None:
    secret_value = "TOP_SECRET_DB_PASSWORD_12345"
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    mgr.register("db_pw", secret_value, kind="password")

    spy = SpyTool({"connected": True})
    spec = ToolSpec(
        name="db_query",
        description="Query DB",
        risk_tier=0,
        required_credentials=["db_pw"],
    )

    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec, spy)

    res = await registry.call("db_query", {"query": "SELECT 1;"})
    assert res.ok is True

    # Check calls list
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call.tool == "db_query"
    assert call.args == {"query": "SELECT 1;"}
    assert "db_pw" not in call.args

    # Check serialization
    call_json = call.model_dump_json()
    assert secret_value not in call_json

    calls_dump = json.dumps([c.model_dump(mode="json") for c in registry.calls])
    assert secret_value not in calls_dump
    assert secret_value not in repr(registry.calls)


# --------------------------------------------------------------------------- #
# Test 8: Ordering: policy -> capability -> credential -> sandbox -> fn
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_governance_ordering_policy_capability_credential_sandbox_fn(
    tmp_path: Path,
) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    # Missing credential intentionally
    spy = SpyTool()
    spec = ToolSpec(
        name="governed_tool",
        description="Governed tool requiring credential and sandbox",
        risk_tier=2,
        required_credentials=["missing_cred"],
        sandboxed=True,
    )

    # 8a: Policy DENY takes precedence over capability, credential, and sandbox
    deny_engine = PolicyEngine(
        [
            PolicyRule(
                id="deny-all",
                effect=PolicyEffect.DENY,
                priority=100,
                action_pattern="governed_tool",
                reason="policy forbids governed_tool",
            )
        ]
    )
    reg_policy = ToolRegistry(
        policy_engine=deny_engine,
        credential_manager=mgr,
    )
    reg_policy.register(spec, spy)

    res_policy = await reg_policy.call("governed_tool", {})
    assert res_policy.ok is False
    # Must fail with policy reason, NOT missing credential reason
    assert "policy forbids governed_tool" in res_policy.error
    assert not spy.called

    # 8b: Capability DENY takes precedence over credential and sandbox
    allow_engine = PolicyEngine(
        [
            PolicyRule(
                id="allow-all",
                effect=PolicyEffect.ALLOW,
                priority=100,
                action_pattern="governed_tool",
                reason="policy allows governed_tool",
            )
        ]
    )
    cap_resolver = CapabilityResolver()  # No grants issued yet
    reg_cap = ToolRegistry(
        policy_engine=allow_engine,
        capability_resolver=cap_resolver,
        credential_manager=mgr,
    )
    reg_cap.register(spec, spy)

    res_cap = await reg_cap.call("governed_tool", {"url": "https://example.com"})
    assert res_cap.ok is False
    # Must fail with capability reason, NOT missing credential reason
    assert "no grant" in res_cap.error
    assert not spy.called

    # 8c: Credential failure takes precedence over sandbox / fn
    cap_resolver_allow = CapabilityResolver()
    cap_resolver_allow.grant("agent", "network")
    reg_cred = ToolRegistry(
        policy_engine=allow_engine,
        capability_resolver=cap_resolver_allow,
        credential_manager=mgr,  # still missing missing_cred
    )
    reg_cred.register(spec, spy)

    res_cred = await reg_cred.call("governed_tool", {"url": "https://example.com"})
    assert res_cred.ok is False
    assert "missing required credential: missing_cred" in res_cred.error
    assert not spy.called

    # 8d: All pass -> runs through sandbox and fn executes
    mgr.register("missing_cred", "valid_secret_123", kind="token")
    sandbox = Sandbox(SandboxPolicy(max_cpu_seconds=1.0, max_output_bytes=1024))
    reg_all = ToolRegistry(
        policy_engine=allow_engine,
        capability_resolver=cap_resolver_allow,
        credential_manager=mgr,
        sandbox=sandbox,
    )
    reg_all.register(spec, spy)

    res_all = await reg_all.call("governed_tool", {"url": "https://example.com"})
    assert res_all.ok is True
    assert spy.called is True
    assert spy.calls[0]["missing_cred"] == "valid_secret_123"

# --------------------------------------------------------------------------- #
# Test 9: Resolution works by both ref_id and name
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_credential_resolution_by_name_and_by_ref_id(
    tmp_path: Path,
) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    ref = mgr.register("service_token", "SECRET_SERVICE_TOKEN_789", kind="token")

    # Tool A requires by name
    spy_a = SpyTool()
    spec_a = ToolSpec(
        name="tool_by_name",
        description="Tool by name",
        risk_tier=0,
        required_credentials=["service_token"],
    )

    # Tool B requires by ref_id
    spy_b = SpyTool()
    spec_b = ToolSpec(
        name="tool_by_ref",
        description="Tool by ref_id",
        risk_tier=0,
        required_credentials=[ref.ref_id],
    )

    registry = ToolRegistry(credential_manager=mgr)
    registry.register(spec_a, spy_a)
    registry.register(spec_b, spy_b)

    res_a = await registry.call("tool_by_name", {})
    assert res_a.ok is True
    assert spy_a.calls[0]["service_token"] == "SECRET_SERVICE_TOKEN_789"

    res_b = await registry.call("tool_by_ref", {})
    assert res_b.ok is True
    # Injected under ref_id and under name
    assert (
        spy_b.calls[0].get("service_token") == "SECRET_SERVICE_TOKEN_789"
        or spy_b.calls[0].get(ref.ref_id) == "SECRET_SERVICE_TOKEN_789"
    )
