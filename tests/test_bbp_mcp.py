"""Tests for real MCP tool integration with the BBP recon workflow.

Covers:
1. Real MCP tool call (find_subdomains_passive) records call and populates findings.
2. Tool failure degrades to stub fallback without crashing.
3. Out-of-scope targets never reach tools.call (scope gate runs first).
4. Real-tool run carries NO simulation marker; stub run carries simulation marker.
5. No tools configured (tools=None) preserves unchanged legacy behavior.
6. Scope gate blocks everything when in_scope is empty.
7. Real MCP probe_http call records call and generates security header findings.
8. Real tool returning empty result produces empty findings (no fabrication).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.tools import ToolRegistry, ToolSpec
from uap.workflows.bbp import BBPWorkflow
from uap.workflows.scope import ScopeGate


def make_task(targets: list[str]) -> TaskSpec:
    return TaskSpec(
        domain=Domain.BBP,
        goal="bbp: assess target",
        input={"targets": targets},
    )


def default_gate() -> ScopeGate:
    return ScopeGate(in_scope=["*.example.com", "example.com"])


# --------------------------------------------------------------------------- #
# Test 1: Real MCP tool call records call and data reaches findings
# --------------------------------------------------------------------------- #


async def test_real_mcp_tool_call_records_call_and_data_reaches_findings():
    registry = ToolRegistry()

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

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["example.com"]))

    assert result.status == WorkflowStatus.COMPLETED

    # Assert via registry.calls that the tool was called
    tool_calls = [c for c in registry.calls if c.tool == "bugbounty-mcp.find_subdomains_passive"]
    assert len(tool_calls) == 1
    assert tool_calls[0].args["domain"] == "example.com"

    # Tool data reaches findings
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    validated = state.data.get("validated_findings", [])
    assert len(validated) == 2
    targets_in_findings = {f["target"] for f in validated}
    assert targets_in_findings == {"api.example.com", "auth.example.com"}

    # Also check artifacts
    raw_findings = next(a for a in result.artifacts if a.type == "findings.json").content_ref
    findings_data = json.loads(raw_findings)
    assert isinstance(findings_data, list)
    assert len(findings_data) == 2


# --------------------------------------------------------------------------- #
# Test 2: Tool failure degrades to stub fallback without crashing
# --------------------------------------------------------------------------- #


async def test_tool_failure_degrades_to_stub_fallback_without_crashing():
    registry = ToolRegistry()

    async def failing_tool(domain: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("crt.sh connection timed out")

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Failing passive recon tool",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        failing_tool,
    )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["example.com"]))

    # Must complete without crashing
    assert result.status == WorkflowStatus.COMPLETED

    # Real tool was invoked and failed
    tool_calls = [c for c in registry.calls if c.tool == "bugbounty-mcp.find_subdomains_passive"]
    assert len(tool_calls) == 1

    # Degraded to stub fallback findings
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    titles = [f["title"] for f in state.data.get("validated_findings", [])]
    assert any("Subdomain takeover candidate on dev.example.com" in t for t in titles)

    # Stub run carries simulation marker
    raw_report = next(a for a in result.artifacts if a.type == "report.md").content_ref
    assert "> **SIMULATION — DETERMINISTIC STUB RECON**" in raw_report
    raw_findings = next(a for a in result.artifacts if a.type == "findings.json").content_ref
    assert "_SIMULATION" in json.loads(raw_findings)


# --------------------------------------------------------------------------- #
# Test 3: Out-of-scope target: tools.call is NEVER invoked
# --------------------------------------------------------------------------- #


async def test_out_of_scope_target_never_invokes_tools_call():
    registry = ToolRegistry()

    async def mock_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        return {"domain": domain, "count": 1, "subdomains": [f"sub.{domain}"]}

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Recon tool",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        mock_subdomains,
    )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["evil.unauthorized.com"]))

    assert result.status == WorkflowStatus.FAILED
    assert "blocked" in (result.error or "")

    # Crucial security invariant: tools.call must NEVER be invoked on out-of-scope target
    assert len(registry.calls) == 0


# --------------------------------------------------------------------------- #
# Test 4: Real-tool run has NO simulation marker; stub run HAS simulation marker
# --------------------------------------------------------------------------- #


async def test_simulation_marker_absent_on_real_run_and_present_on_stub_run():
    # 4A: Real-tool run
    registry = ToolRegistry()

    async def mock_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        return {"domain": domain, "count": 1, "subdomains": [f"api.{domain}"]}

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Recon tool",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        mock_subdomains,
    )

    real_wf = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    real_result = await real_wf.run(make_task(["example.com"]))
    assert real_result.status == WorkflowStatus.COMPLETED

    real_report = next(a for a in real_result.artifacts if a.type == "report.md").content_ref
    assert "SIMULATION — DETERMINISTIC STUB RECON" not in real_report

    real_findings = next(a for a in real_result.artifacts if a.type == "findings.json").content_ref
    parsed_real = json.loads(real_findings)
    assert isinstance(parsed_real, list), "Real-tool findings artifact must be a bare list"
    assert "_SIMULATION" not in parsed_real

    # 4B: Stub run (no tools configured)
    stub_wf = BBPWorkflow(scope_gate=default_gate(), tools=None)
    stub_result = await stub_wf.run(make_task(["example.com"]))
    assert stub_result.status == WorkflowStatus.COMPLETED

    stub_report = next(a for a in stub_result.artifacts if a.type == "report.md").content_ref
    assert "> **SIMULATION — DETERMINISTIC STUB RECON**" in stub_report

    stub_findings = next(a for a in stub_result.artifacts if a.type == "findings.json").content_ref
    parsed_stub = json.loads(stub_findings)
    assert isinstance(parsed_stub, dict)
    assert "_SIMULATION" in parsed_stub


# --------------------------------------------------------------------------- #
# Test 5: No tools configured: unchanged legacy behavior
# --------------------------------------------------------------------------- #


async def test_no_tools_configured_unchanged_legacy_behavior():
    workflow = BBPWorkflow(scope_gate=default_gate(), tools=None)
    result = await workflow.run(make_task(["api.example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    assert [a.type for a in result.artifacts] == ["findings.json", "report.md"]
    assert result.verification is not None
    assert result.verification.passed is True

    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert len(state.data["validated_findings"]) == 4

    findings_json = next(a for a in result.artifacts if a.type == "findings.json").content_ref
    payload = json.loads(findings_json)
    assert "_SIMULATION" in payload
    assert len(payload["findings"]) == 4


# --------------------------------------------------------------------------- #
# Test 6: Scope gate blocks everything when in_scope is empty
# --------------------------------------------------------------------------- #


async def test_scope_gate_blocks_everything_when_in_scope_is_empty():
    registry = ToolRegistry()

    async def mock_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        return {"domain": domain, "count": 1, "subdomains": [f"api.{domain}"]}

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Recon tool",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        mock_subdomains,
    )

    empty_gate = ScopeGate(in_scope=[])
    workflow = BBPWorkflow(scope_gate=empty_gate, tools=registry)
    result = await workflow.run(make_task(["example.com"]))
    assert result.status == WorkflowStatus.FAILED
    assert "blocked" in (result.error or "")
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert "no in-scope rules defined" in state.data["blocked_targets"][0]["reason"]
    # Hard gate invariant: tools.call is never reached
    assert len(registry.calls) == 0


# --------------------------------------------------------------------------- #
# Test 7: Real MCP probe_http maps to security header findings
# --------------------------------------------------------------------------- #


async def test_probe_http_real_mcp_tool_maps_to_findings():
    registry = ToolRegistry()

    async def mock_probe_http(url: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": url,
            "final_url": url,
            "status_code": 200,
            "title": "Welcome Home",
            "headers": {
                "Server": "nginx",
                # Missing CSP, HSTS, X-Frame-Options
            },
        }

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.probe_http",
            description="Probe HTTP endpoint",
            risk_tier=2,
            input_schema={"url": {"type": "string", "required": True}},
        ),
        mock_probe_http,
    )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    assert any(c.tool == "bugbounty-mcp.probe_http" for c in registry.calls)

    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    validated = state.data.get("validated_findings", [])
    titles = [f["title"] for f in validated]
    assert any("Missing security header Content-Security-Policy" in t for t in titles)

    report = next(a for a in result.artifacts if a.type == "report.md").content_ref
    assert "SIMULATION — DETERMINISTIC STUB RECON" not in report


# --------------------------------------------------------------------------- #
# Test 8: Empty tool result produces no fabricated findings
# --------------------------------------------------------------------------- #


async def test_empty_tool_result_produces_no_fabricated_findings():
    registry = ToolRegistry()

    async def empty_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        # Tool queried crt.sh and found 0 subdomains
        return {"domain": domain, "count": 0, "subdomains": []}

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Passive recon returning nothing",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        empty_subdomains,
    )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    # No fabrication: findings list is empty for this step!
    assert state.data.get("validated_findings") == []

    report = next(a for a in result.artifacts if a.type == "report.md").content_ref
    assert "SIMULATION — DETERMINISTIC STUB RECON" not in report
    assert "- none" in report

# --------------------------------------------------------------------------- #
# Test 9: Event bus receives MCP_CALL event on real tool invocation
# --------------------------------------------------------------------------- #


async def test_event_bus_receives_mcp_call_event():
    from uap.observability.events import EventBus, EventKind

    registry = ToolRegistry()
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    registry.event_bus = bus

    async def mock_subdomains(domain: str, **kwargs: Any) -> dict[str, Any]:
        return {"domain": domain, "count": 1, "subdomains": [f"api.{domain}"]}

    registry.register(
        ToolSpec(
            name="bugbounty-mcp.find_subdomains_passive",
            description="Recon tool",
            risk_tier=1,
            input_schema={"domain": {"type": "string", "required": True}},
        ),
        mock_subdomains,
    )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    mcp_events = [e for e in events if e.kind == EventKind.MCP_CALL]
    assert len(mcp_events) >= 1
    assert mcp_events[0].data["tool"] == "bugbounty-mcp.find_subdomains_passive"
    assert mcp_events[0].data["ok"] is True
