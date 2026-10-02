"""Tests for build Step 7 - Tool Registry (Master sections 9, 12, 13, 20)."""

import json
from pathlib import Path

import pytest

from uap.contracts import ApprovalRequest, ApprovalState, ToolResult
from uap.tools import (
    PolicyDecision,
    ToolPolicy,
    ToolRegistry,
    ToolSpec,
    register_local_tools,
)


def _spec(name: str, *, risk_tier: int = 0, required: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"test tool {name}",
        risk_tier=risk_tier,
        input_schema={k: {"type": "string", "required": True} for k in required},
    )


def _approved(task_id: str = "task_1", action: str = "write artifact") -> ApprovalRequest:
    return ApprovalRequest(
        task_id=task_id,
        action=action,
        state=ApprovalState.APPROVED,
        decided_by="tester",
    )


def _registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_local_tools(registry, root)
    return registry


# --------------------------------------------------------------------------- #
# Registry surface
# --------------------------------------------------------------------------- #


async def test_register_get_list():
    registry = ToolRegistry()
    registry.register(_spec("alpha"), _async_identity)
    registry.register(_spec("beta"), _async_identity)

    assert [s.name for s in registry.list()] == ["alpha", "beta"]
    entry = registry.get("alpha")
    assert entry is not None and entry[0].name == "alpha" and entry[1] is _async_identity
    assert registry.get("nope") is None


async def test_duplicate_registration_raises():
    registry = ToolRegistry()
    registry.register(_spec("alpha"), _async_identity)
    with pytest.raises(ValueError, match="already registered: alpha"):
        registry.register(_spec("alpha"), _async_identity)


async def test_unknown_tool_returns_error_and_never_raises():
    registry = ToolRegistry()
    result = await registry.call("does_not_exist", {"x": 1})
    assert result.ok is False
    assert "does_not_exist" in result.error
    assert result.tool == "does_not_exist"
    assert result.duration_ms >= 0


async def test_missing_required_arg():
    registry = ToolRegistry()
    registry.register(_spec("alpha", required=("path", "content")), _async_identity)
    result = await registry.call("alpha", {"path": "a"})
    assert result.ok is False
    assert "missing required arg" in (result.error or "")
    assert "content" in (result.error or "")


async def test_successful_call_preserves_result_and_timing():
    registry = ToolRegistry()
    registry.register(_spec("alpha"), _async_identity)
    result = await registry.call("alpha", {"hello": "world"})
    assert result.ok is True
    assert result.result == {"hello": "world"}
    assert result.error is None
    assert result.duration_ms >= 0


async def test_call_id_matches_recorded_call():
    registry = ToolRegistry()
    registry.register(_spec("alpha"), _async_identity)
    result = await registry.call("alpha", {"hello": "world"})
    assert registry.calls
    assert registry.calls[-1].call_id == result.call_id
    assert registry.calls[-1].args == {"hello": "world"}


# --------------------------------------------------------------------------- #
# Policy guard
# --------------------------------------------------------------------------- #


def test_tier_3_without_approval_needs_approval_and_blocks():
    decision = ToolPolicy().check(_spec("boom", risk_tier=3), None)
    assert isinstance(decision, PolicyDecision)
    assert decision.allowed is False
    assert decision.needs_approval is True
    assert "approved request" in decision.reason


def test_tier_3_approval_states():
    policy = ToolPolicy()
    spec = _spec("boom", risk_tier=3)

    pending = ApprovalRequest(task_id="t", action="a")
    assert policy.check(spec, pending).allowed is False
    assert policy.check(spec, pending).needs_approval is True

    rejected = pending.model_copy(update={"state": ApprovalState.REJECTED})
    rejected_decision = policy.check(spec, rejected)
    assert rejected_decision.allowed is False
    assert rejected_decision.needs_approval is False

    approved = policy.check(spec, _approved())
    assert approved.allowed is True
    assert approved.needs_approval is False
    assert "tester" in approved.reason


def test_tier_0_allowed_without_approval():
    decision = ToolPolicy().check(_spec("safe", risk_tier=0), None)
    assert decision.allowed is True
    assert decision.needs_approval is False


def test_allowed_tiers_restricts_lower_tiers():
    policy = ToolPolicy(allowed_tiers={0})
    assert policy.check(_spec("dns", risk_tier=1), None).allowed is False
    assert policy.check(_spec("local", risk_tier=0), None).allowed is True


async def test_registry_enforces_policy_on_call():
    registry = ToolRegistry()
    registry.register(_spec("boom", risk_tier=3), _async_identity)
    result = await registry.call("boom", {})
    assert result.ok is False
    assert "approved request" in (result.error or "")


# --------------------------------------------------------------------------- #
# Local filesystem tools
# --------------------------------------------------------------------------- #


async def test_write_artifact_file_and_escape_attempt(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    registry = _registry(root)

    result = await registry.call(
        "write_artifact_file",
        {"path": "sub/out.txt", "content": "hello"},
        approval=_approved(),
    )
    assert result.ok is True, result.error
    written = Path(result.result)
    assert written.is_relative_to(root)
    assert written.read_text(encoding="utf-8") == "hello"

    escape = await registry.call(
        "write_artifact_file",
        {"path": "../evil.txt", "content": "pwned"},
        approval=_approved(),
    )
    assert escape.ok is False
    assert "escapes the artifacts root" in (escape.error or "")
    assert (tmp_path / "evil.txt").exists() is False


async def test_read_text_file_scope_and_size_limit(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    registry = _registry(root)

    outside = await registry.call("read_text_file", {"path": "../secret.txt"})
    assert outside.ok is False
    assert "escapes the artifacts root" in (outside.error or "")

    (root / "note.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    read = await registry.call("read_text_file", {"path": "note.txt"})
    assert read.ok is True, read.error
    assert read.result == "first line\nsecond line\n"

    big = "x" * (64 * 1024 + 2048)
    (root / "big.txt").write_text(big, encoding="utf-8")
    truncated = await registry.call("read_text_file", {"path": "big.txt"})
    assert truncated.ok is True
    assert len(truncated.result) == 64 * 1024
    assert truncated.result == "x" * (64 * 1024)


async def test_write_without_approval_is_blocked(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    registry = _registry(root)
    result = await registry.call("write_artifact_file", {"path": "a.txt", "content": "nope"})
    assert result.ok is False
    assert (root / "a.txt").exists() is False


async def test_approved_tier_3_end_to_end(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    registry = _registry(root)

    written = await registry.call(
        "write_artifact_file",
        {"path": "report.md", "content": "# Findings\n"},
        approval=_approved(action="publish report.md"),
    )
    assert written.ok is True, written.error

    read_back = await registry.call("read_text_file", {"path": "report.md"})
    assert read_back.ok is True, read_back.error
    assert read_back.result == "# Findings\n"
    assert (root / "report.md").read_text(encoding="utf-8") == "# Findings\n"


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


async def test_tool_result_round_trips_json():
    registry = ToolRegistry()
    registry.register(_spec("alpha"), _async_identity)
    result = await registry.call("alpha", {"a": 1, "b": [1, 2]})

    payload = result.model_dump_json()
    assert json.loads(payload)["ok"] is True
    restored = ToolResult.model_validate_json(payload)
    assert restored == result


async def _async_identity(**kwargs):
    return kwargs
