"""Durable BBP workflow tests on the §71 vertical slice.

Verifies:
1. A BBP run through the slice completes and its artifacts are real (not fixtures) when tools are configured.
2. The scope gate blocks: an out-of-scope target -> run fails closed, and a spy ToolRegistry records ZERO calls.
3. The same invariant holds on the legacy path (regression guard).
4. The run's node_history reflects the real BBP pipeline nodes.
5. The run is durable (an executions row exists for it).
6. Research is unaffected (regression).
7. End-to-end scope gate failure via the HTTP server path fails closed with ZERO tool calls.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
import uuid

import pytest
from fastapi.testclient import TestClient

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.db.engine import session_scope
from uap.db.models import Execution, ExecutionStatus
from uap.runtime.service import ExecutionService
from uap.server import create_app
from uap.slice import PlatformSlice
from uap.tools import ToolRegistry, ToolSpec
from uap.workflows.bbp import BBPWorkflow
from uap.workflows.scope import ScopeGate


DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


def _db_skip_reason() -> str | None:
    try:
        from sqlalchemy import text
        from uap.db import create_db_engine

        engine = create_db_engine(
            _resolved_test_url(), connect_args={"connect_timeout": 2}
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {_resolved_test_url()}: {exc}"
    return None


_SKIP = _db_skip_reason()
db_test = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")



class SpyToolRegistry(ToolRegistry):
    """ToolRegistry spy that tracks every invocation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.call_count: int = 0

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        return await super().call(name, *args, **kwargs)


def _make_real_tool_registry() -> SpyToolRegistry:
    registry = SpyToolRegistry()

    async def mock_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "domain": domain,
            "count": 2,
            "subdomains": [f"api.{domain}", f"auth.{domain}"],
        }

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Passively discover subdomains from CT logs",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        mock_subdomains,
    )
    return registry


# --------------------------------------------------------------------------- #
# 1. A BBP run through the slice completes and artifacts are real (not fixtures)
# --------------------------------------------------------------------------- #


@db_test
def test_bbp_slice_completes_with_real_artifacts(db_session_factory, tmp_path: Path):
    registry = _make_real_tool_registry()
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
        tools=registry,
    )
    gate = ScopeGate(in_scope=["*.example.com", "example.com"])
    result = slice_.run(
        "run bbp recon against target example.com",
        scope_gate=gate,
    )

    assert result.error is None, result.error
    assert result.execution_status == "completed"
    assert registry.call_count == 1
    assert len(registry.calls) == 1
    assert registry.calls[0].tool == "bugbounty-mcp.find_subdomains_passive"

    # Artifacts are real, not fixtures (no _SIMULATION marker)
    findings_files = [p for p in tmp_path.rglob("*findings.json*") if not p.name.endswith(".meta.json")]
    assert findings_files, "expected findings.json artifact on disk"
    content = findings_files[0].read_text(encoding="utf-8")
    assert "_SIMULATION" not in content, "expected real output, not simulation fixture"
    parsed = json.loads(content)
    assert isinstance(parsed, list)
    targets = {f["target"] for f in parsed}
    assert "api.example.com" in targets
    assert "auth.example.com" in targets

    report_files = [p for p in tmp_path.rglob("*report.md*") if not p.name.endswith(".meta.json")]
    assert report_files, "expected report.md artifact on disk"
    report_text = report_files[0].read_text(encoding="utf-8")
    assert "[SIMULATION]" not in report_text


# --------------------------------------------------------------------------- #
# 2. Scope gate blocks: out-of-scope target -> fails closed, ZERO tool calls
# --------------------------------------------------------------------------- #


@db_test
def test_scope_gate_blocks_out_of_scope_target_in_slice(db_session_factory, tmp_path: Path):
    registry = _make_real_tool_registry()
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
        tools=registry,
    )
    gate = ScopeGate(in_scope=["*.example.com"])
    # evil.unauthorized.com is out-of-scope
    result = slice_.run(
        "run bbp recon against target evil.unauthorized.com",
        scope_gate=gate,
    )

    assert result.execution_status == "failed"
    assert "blocked" in (result.error or "")
    # THE SECURITY INVARIANT: tool must NEVER be invoked
    assert registry.call_count == 0
    assert len(registry.calls) == 0
    # No node was ever executed: refused before enqueueing
    assert result.execution_id is None


# --------------------------------------------------------------------------- #
# 3. Regression guard: same scope invariant holds on the legacy path
# --------------------------------------------------------------------------- #


def test_scope_gate_blocks_out_of_scope_target_in_legacy_path():
    registry = _make_real_tool_registry()
    gate = ScopeGate(in_scope=["*.example.com"])
    workflow = BBPWorkflow(scope_gate=gate, tools=registry)
    task = TaskSpec(
        domain=Domain.BBP,
        goal="bbp: assess target evil.unauthorized.com",
        input={"targets": ["evil.unauthorized.com"]},
    )
    result = asyncio.run(workflow.run(task))

    assert result.status == WorkflowStatus.FAILED
    assert "blocked" in (result.error or "")
    # THE SECURITY INVARIANT on legacy path: tool is NEVER invoked
    assert registry.call_count == 0
    assert len(registry.calls) == 0


# --------------------------------------------------------------------------- #
# 4. The run's node_history reflects the real BBP pipeline nodes
# --------------------------------------------------------------------------- #


@db_test
def test_bbp_slice_node_history_reflects_bbp_nodes(db_session_factory, tmp_path: Path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
    )
    gate = ScopeGate(in_scope=["*.example.com", "example.com"])
    result = slice_.run(
        "run bbp recon against target example.com",
        scope_gate=gate,
    )

    assert result.error is None, result.error
    assert result.execution_id is not None

    from uap.db.repositories import EventRepository

    with session_scope(db_session_factory) as session:
        rows = EventRepository(session).read_since(
            uuid.UUID(str(result.execution_id)), 0
        )
    node_history = [
        str(e.node)
        for e in rows
        if e.kind == "node_started" and e.node
    ]
    expected_bbp_nodes = [
        "scope_validation",
        "recon_planning",
        "asset_discovery",
        "endpoint_discovery",
        "finding_generation",
        "finding_classification",
        "validation",
        "report",
    ]
    for expected in expected_bbp_nodes:
        assert expected in node_history, f"expected node {expected} in node_history {node_history}"


# --------------------------------------------------------------------------- #
# 5. The run is durable (an executions row exists for it)
# --------------------------------------------------------------------------- #


@db_test
def test_bbp_slice_run_is_durable(db_session_factory, tmp_path: Path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
    )
    gate = ScopeGate(in_scope=["*.example.com", "example.com"])
    result = slice_.run(
        "run bbp recon against target example.com",
        scope_gate=gate,
    )

    assert result.error is None, result.error
    assert result.execution_id is not None

    with session_scope(db_session_factory) as session:
        row = session.get(Execution, uuid.UUID(str(result.execution_id)))
        assert row is not None
        assert row.status == ExecutionStatus.COMPLETED
        assert row.correlation_id is not None
        assert row.input is not None


# --------------------------------------------------------------------------- #
# 6. Research is unaffected (regression guard)
# --------------------------------------------------------------------------- #


@db_test
def test_research_slice_unaffected(db_session_factory, tmp_path: Path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
    )
    result = slice_.run("research and compare two approaches to caching")

    assert result.error is None, result.error
    assert result.execution_status == "completed"
    assert result.workflow_ref == "research-slice@v1"
    assert len(result.artifacts) > 0
    assert result.traces_recorded >= 1
    assert result.events_emitted > 0


# --------------------------------------------------------------------------- #
# 7. End-to-end HTTP server: out-of-scope fails closed, zero tool calls
# --------------------------------------------------------------------------- #


def test_scope_gate_blocks_via_http_server_zero_tool_calls(tmp_path: Path):
    registry = _make_real_tool_registry()
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    if app.state.slice is not None:
        app.state.slice.tools = registry
    app.state.mcp_tools = registry

    client = TestClient(app)
    resp = client.post(
        "/tasks",
        json={
            "input": "Bug bounty on evil.unauthorized.com. In scope: *.example.com",
            "user_id": "tester",
        },
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["status"] == "failed"
    assert "blocked" in (detail.get("error") or "")
    assert registry.call_count == 0
    assert len(registry.calls) == 0
