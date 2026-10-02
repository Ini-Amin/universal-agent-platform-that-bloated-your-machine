"""Security layer tests (Master sections 29-32): capability, policy,
credentials, sandbox. Deterministic, network-free, no OS sandbox assumptions.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from uap.capability import Capability, CapabilityGrant, CapabilityResolver
from uap.contracts import utc_now
from uap.credentials import CredentialManager, CredentialRef
from uap.policy import PolicyEffect, PolicyEngine, PolicyRule
from uap.sandbox import Sandbox, SandboxPolicy, SandboxViolation


# --------------------------------------------------------------------------- #
# Package imports
# --------------------------------------------------------------------------- #


def test_all_four_packages_import() -> None:
    import uap.capability
    import uap.credentials
    import uap.policy
    import uap.sandbox

    assert uap.capability and uap.credentials and uap.policy and uap.sandbox


# --------------------------------------------------------------------------- #
# Capability resolver (section 29)
# --------------------------------------------------------------------------- #


def test_grant_and_check_allowed() -> None:
    r = CapabilityResolver()
    r.grant("agent:x", "filesystem")
    ok, reason = r.check("agent:x", "filesystem")
    assert ok and reason == "allowed"


def test_no_grant_denied() -> None:
    r = CapabilityResolver()
    ok, reason = r.check("agent:x", "network")
    assert not ok
    assert reason == "no grant for agent:x/network"


def test_expired_grant_denied() -> None:
    r = CapabilityResolver()
    g = r.grant("s", "filesystem", ttl_s=1)
    future = g.expires_at + timedelta(seconds=5)
    ok, reason = r.check("s", "filesystem", now=future)
    assert not ok and reason == "grant expired"


def test_revoked_grant_denied() -> None:
    r = CapabilityResolver()
    g = r.grant("s", "filesystem")
    r.revoke(g.grant_id)
    ok, reason = r.check("s", "filesystem")
    assert not ok and reason == "grant revoked"


def test_scope_mismatch_match_and_wildcard() -> None:
    r = CapabilityResolver()
    r.grant("s", "filesystem", scopes=["/tmp"])
    # mismatch denied
    ok, reason = r.check("s", "filesystem", scope="/etc")
    assert not ok and reason == "scope /etc not granted"
    # match allowed
    ok, reason = r.check("s", "filesystem", scope="/tmp")
    assert ok and reason == "allowed"
    # empty scopes = wildcard covers any scope
    r.grant("w", "network")  # no scopes
    ok, reason = r.check("w", "network", scope="example.com")
    assert ok and reason == "allowed"


def test_grants_for_filtered() -> None:
    r = CapabilityResolver()
    r.grant("a", "filesystem")
    r.grant("a", "network")
    r.grant("b", "filesystem")
    subjects = {g.capability for g in r.grants_for("a")}
    assert subjects == {"filesystem", "network"}
    assert len(r.grants_for("b")) == 1


def test_revoke_unknown_raises_keyerror() -> None:
    r = CapabilityResolver()
    with pytest.raises(KeyError):
        r.revoke("does-not-exist")


# --------------------------------------------------------------------------- #
# Scoped tokens
# --------------------------------------------------------------------------- #


def test_token_round_trip_valid_and_payload() -> None:
    r = CapabilityResolver(secret="stable-test-secret")
    token = r.issue_scoped_token("s", "network", scope="example.com", ttl_s=300)
    assert token.startswith("uap.")
    valid, reason, payload = r.check_token(token)
    assert valid and reason == "valid"
    assert payload["subject"] == "s"
    assert payload["capability"] == "network"
    assert payload["scope"] == "example.com"


def test_tampered_token_invalid() -> None:
    r = CapabilityResolver(secret="stable-test-secret")
    token = r.issue_scoped_token("s", "network")
    prefix, body, sig = token.split(".")
    # flip one char in the signature
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    valid, reason, payload = r.check_token(f"{prefix}.{body}.{bad_sig}")
    assert not valid and reason == "bad signature" and payload is None


def test_other_instance_token_invalid() -> None:
    issuer = CapabilityResolver(secret="secret-A")
    verifier = CapabilityResolver(secret="secret-B")
    token = issuer.issue_scoped_token("s", "network")
    valid, reason, _ = verifier.check_token(token)
    assert not valid and reason == "bad signature"


def test_expired_token_invalid() -> None:
    r = CapabilityResolver(secret="stable-test-secret")
    token = r.issue_scoped_token("s", "network", ttl_s=-1)  # already expired
    valid, reason, payload = r.check_token(token)
    assert not valid and reason == "token expired"
    assert payload is not None  # payload still returned for audit


# --------------------------------------------------------------------------- #
# Capability model
# --------------------------------------------------------------------------- #


def test_capability_grant_active_helper() -> None:
    cap = Capability(name="filesystem", risk_tier=1, scopes=("/tmp",))
    assert cap.risk_tier == 1
    g = CapabilityGrant(subject="s", capability="filesystem")
    assert g.active() is True
    g2 = g.model_copy(update={"revoked": True})
    assert g2.active() is False


# --------------------------------------------------------------------------- #
# Policy engine (section 31)
# --------------------------------------------------------------------------- #


def test_policy_highest_priority_wins() -> None:
    engine = PolicyEngine(
        [
            PolicyRule(id="low", effect=PolicyEffect.DENY, priority=1),
            PolicyRule(id="high", effect=PolicyEffect.ALLOW, priority=10),
        ]
    )
    effect, _ = engine.decide("s", "a", "r")
    assert effect == PolicyEffect.ALLOW


def test_policy_equal_priority_deny_beats_allow() -> None:
    engine = PolicyEngine(
        [
            PolicyRule(id="allow", effect=PolicyEffect.ALLOW, priority=5),
            PolicyRule(id="deny", effect=PolicyEffect.DENY, priority=5),
        ]
    )
    effect, _ = engine.decide("s", "a", "r")
    assert effect == PolicyEffect.DENY


def test_policy_no_match_asks() -> None:
    engine = PolicyEngine(
        [PolicyRule(id="only", effect=PolicyEffect.ALLOW, action_pattern="fs:read")]
    )
    effect, reason = engine.decide("s", "net:outbound", "r")
    assert effect == PolicyEffect.ASK and reason == "no rule matched"


def test_policy_default_rules() -> None:
    engine = PolicyEngine.default()
    # denies secrets regardless of action
    effect, _ = engine.decide("s", "fs:read", "config/app_secret.yaml")
    assert effect == PolicyEffect.DENY
    # allows plain fs:read
    effect, _ = engine.decide("s", "fs:read", "src/main.py")
    assert effect == PolicyEffect.ALLOW
    # asks fs:write
    effect, _ = engine.decide("s", "fs:write", "src/main.py")
    assert effect == PolicyEffect.ASK
    # .env denied even on read
    effect, _ = engine.decide("s", "fs:read", ".env")
    assert effect == PolicyEffect.DENY


def test_policy_tiebreak_by_rule_id() -> None:
    # same priority, same effect severity -> lexicographically smallest id wins
    engine = PolicyEngine(
        [
            PolicyRule(id="bbb", effect=PolicyEffect.ALLOW, priority=1, reason="b"),
            PolicyRule(id="aaa", effect=PolicyEffect.ALLOW, priority=1, reason="a"),
        ]
    )
    _, reason = engine.decide("s", "x", "y")
    assert reason == "a"


# --------------------------------------------------------------------------- #
# Credential manager (section 32)
# --------------------------------------------------------------------------- #


def test_credentials_register_resolve_rotate(tmp_path) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    ref = mgr.register("github", "ghp_SECRET_VALUE", kind="token", scope="global")
    assert isinstance(ref, CredentialRef)
    assert ref.version == 1
    assert mgr.resolve(ref.ref_id) == "ghp_SECRET_VALUE"
    updated = mgr.rotate(ref.ref_id, "ghp_ROTATED_VALUE")
    assert updated.version == 2 and updated.rotated_at is not None
    assert mgr.resolve(ref.ref_id) == "ghp_ROTATED_VALUE"


def test_ref_serialization_never_contains_secret(tmp_path) -> None:
    secret = "SUPER_SECRET_TOKEN_12345"
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    ref = mgr.register("api", secret, kind="apikey")
    assert secret not in ref.model_dump_json()
    assert secret not in repr(ref)
    assert secret not in repr(mgr)
    # no field on the ref holds the value
    assert secret not in json.dumps(ref.model_dump(mode="json"))


def test_credentials_delete_then_resolve_keyerror(tmp_path) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    ref = mgr.register("db", "pw", kind="password")
    mgr.delete(ref.ref_id)
    with pytest.raises(KeyError):
        mgr.resolve(ref.ref_id)


def test_credentials_redact_and_list(tmp_path) -> None:
    mgr = CredentialManager(store_path=tmp_path / "creds.json")
    mgr.register("github", "ghp_abc123", kind="token")
    assert len(mgr.list_refs()) == 1
    redacted = mgr.redact("curl -H 'auth: ghp_abc123' https://api")
    assert "ghp_abc123" not in redacted
    assert "[REDACTED:github]" in redacted


def test_credentials_persist_across_instances(tmp_path) -> None:
    path = tmp_path / "creds.json"
    mgr = CredentialManager(store_path=path)
    ref = mgr.register("svc", "val", kind="token")
    # reload from disk in a fresh manager
    mgr2 = CredentialManager(store_path=path)
    assert mgr2.resolve(ref.ref_id) == "val"
    assert [r.name for r in mgr2.list_refs()] == ["svc"]


# --------------------------------------------------------------------------- #
# Sandbox (section 30)
# --------------------------------------------------------------------------- #


def test_sandbox_path_inside_and_outside(tmp_path) -> None:
    allowed = tmp_path / "work"
    allowed.mkdir()
    sb = Sandbox(
        SandboxPolicy(
            allow_fs_write_paths=(str(allowed),),
            allow_fs_read_paths=(str(allowed),),
        )
    )
    inside = sb.check_path(allowed / "a.txt", write=True)
    assert inside == (allowed / "a.txt").resolve()
    with pytest.raises(SandboxViolation):
        sb.check_path("/etc/passwd", write=False)


def test_sandbox_network_rules() -> None:
    # 1. network disabled -> any host denied
    sb = Sandbox(SandboxPolicy(allow_network=False))
    with pytest.raises(SandboxViolation):
        sb.check_network("example.com")
    # 2. enabled with explicit allowlist
    sb = Sandbox(SandboxPolicy(allow_network=True, allow_network_hosts=("example.com",)))
    assert sb.check_network("example.com") == "example.com"
    with pytest.raises(SandboxViolation):
        sb.check_network("evil.com")
    # 3. enabled with empty allowlist -> any host allowed
    sb = Sandbox(SandboxPolicy(allow_network=True))
    assert sb.check_network("anything.com") == "anything.com"


def test_sandbox_subprocess_rule() -> None:
    sb = Sandbox(SandboxPolicy(allow_subprocess=False))
    with pytest.raises(SandboxViolation):
        sb.check_subprocess()
    sb = Sandbox(SandboxPolicy(allow_subprocess=True))
    assert sb.check_subprocess() is None


def test_sandbox_truncate_output_str_and_bytes() -> None:
    sb = Sandbox(SandboxPolicy(max_output_bytes=5))
    assert sb.truncate_output("abcdefghij") == "abcde"
    assert sb.truncate_output(b"abcdefghij") == b"abcde"
    # under limit untouched
    assert sb.truncate_output("ab") == "ab"
    assert sb.truncate_output(b"ab") == b"ab"


def test_sandbox_run_guarded_timeout() -> None:
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=0.05))

    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "done"

    with pytest.raises(SandboxViolation):
        asyncio.run(sb.run_guarded(slow))


def test_sandbox_run_guarded_exception_passthrough() -> None:
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=1.0))

    async def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        asyncio.run(sb.run_guarded(boom))


def test_sandbox_run_guarded_truncates_result() -> None:
    sb = Sandbox(SandboxPolicy(max_cpu_seconds=1.0, max_output_bytes=3))

    async def big() -> str:
        return "abcdefg"

    assert asyncio.run(sb.run_guarded(big)) == "abc"
