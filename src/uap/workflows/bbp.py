"""Bug Bounty / Security Workflow (Master section 9).

    scope_validation -> recon_planning -> asset_discovery -> endpoint_discovery
        -> finding_generation -> finding_classification -> validation -> report

The pipeline is deterministic and hermetic by default: the scope gate is a hard,
policy-outside-the-model boundary (``scope.py``), the recon sources are injectable
async stubs, and every node is a pure function of the checkpointed state. The
real MCP-backed recon tools land behind the same ``recon_steps`` signature.

Design rules enforced here:

* the scope gate runs **first** and refuses to run at all when absent;
* out-of-scope targets are removed before any recon step is invoked;
* a zero-target run ends ``failed`` without probing anything;
* a denied tool call is recorded and its step skipped, never a crash;
* validation is deterministic: no target/evidence -> no report entry.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from uap.contracts.models import (
    Artifact,
    ArtifactStatus,
    TaskSpec,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
)
from uap.verification.verifier import Criterion, DeterministicVerifier
from uap.workflows.runner import NodeResult, StateStore, WorkflowRunner
from uap.workflows.scope import ScopeGate, normalize_target

log = logging.getLogger(__name__)
if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from uap.tools.registry import ToolRegistry

ReconStep = Callable[[str, dict], Awaitable[dict]]
Finding = dict[str, Any]

WORKFLOW_NAME = "bbp"

NODES: tuple[str, ...] = (
    "scope_validation",
    "recon_planning",
    "asset_discovery",
    "endpoint_discovery",
    "finding_generation",
    "finding_classification",
    "validation",
    "report",
)

_ARTIFACT_SOURCE = "bbp_workflow"

# --------------------------------------------------------------------------- #
# Severity keyword rules (deterministic classification, Master section 9)
# --------------------------------------------------------------------------- #

HIGH_KEYWORDS: tuple[str, ...] = (
    "takeover",
    "rce",
    "remote code execution",
    "sql injection",
    "sqli",
    "ssrf",
    "command injection",
    "deserialization",
    "authentication bypass",
    "auth bypass",
    "arbitrary file",
)
MEDIUM_KEYWORDS: tuple[str, ...] = (
    "xss",
    "cross-site scripting",
    "open redirect",
    "idor",
    "csrf",
    "path traversal",
    "directory traversal",
    "lfi",
    "sensitive data",
    "information disclosure",
    "misconfiguration",
)
LOW_KEYWORDS: tuple[str, ...] = (
    "cors",
    "header",
    "missing",
    "banner",
    "cookie",
    "tls",
    "certificate",
    "information",
    "info",
)


def classify_severity(finding: Finding) -> str:
    """Map a finding's title/detail text to ``high`` / ``medium`` / ``low`` / ``info``."""
    haystack = " ".join(
        str(finding.get(key, "")) for key in ("title", "name", "detail", "summary")
    ).casefold()
    for keyword in HIGH_KEYWORDS:
        if keyword in haystack:
            return "high"
    for keyword in MEDIUM_KEYWORDS:
        if keyword in haystack:
            return "medium"
    for keyword in LOW_KEYWORDS:
        if keyword in haystack:
            return "low"
    return "info"


# --------------------------------------------------------------------------- #
# Deterministic stub recon steps (replaced by real tools in Step 8+)
# --------------------------------------------------------------------------- #

def _fixture_subdomain_recon(target: str) -> dict:
    return {
        "assets": [
            {"host": f"api.{target}", "kind": "subdomain"},
            {"host": f"dev.{target}", "kind": "subdomain"},
        ],
        "endpoints": [{"url": f"https://api.{target}/v1/health", "method": "GET"}],
        "findings": [
            {
                "title": f"Subdomain takeover candidate on dev.{target}",
                "target": f"dev.{target}",
                "evidence": {
                    "detail": "CNAME points at an unclaimed cloud host",
                    "status": 404,
                    "response": "NoSuchBucket",
                },
                "source": "stub_subdomain_recon",
            },
            {
                "title": f"Missing security header on api.{target}",
                "target": f"api.{target}",
                "evidence": {
                    "detail": "X-Frame-Options is absent",
                    "status": 200,
                    "response": "OK",
                },
                "source": "stub_subdomain_recon",
            },
        ],
    }


def _fixture_endpoint_recon(target: str) -> dict:
    return {
        "endpoints": [
            {"url": f"https://{target}/login", "method": "POST"},
            {"url": f"https://{target}/api/users", "method": "GET"},
        ],
        "findings": [
            {
                "title": f"CORS misconfiguration on {target}",
                "target": target,
                "evidence": {
                    "detail": "Access-Control-Allow-Origin reflects the request Origin",
                    "status": 200,
                    "response": "Access-Control-Allow-Origin: https://attacker.example",
                },
                "source": "stub_endpoint_recon",
            },
        ],
    }


def _fixture_http_recon(target: str) -> dict:
    return {
        "findings": [
            {
                "title": f"Reflected XSS on {target}/search",
                "target": target,
                "evidence": {
                    "detail": "the q parameter is reflected unescaped",
                    "status": 200,
                    "response": "<script>alert(1)</script>",
                },
                "source": "stub_http_recon",
            },
        ],
    }


RECON_TOOL_MAP: dict[str, list[str]] = {
    "stub_subdomain_recon": ["bugbounty-mcp.find_subdomains_passive", "find_subdomains_passive"],
    "subdomain_recon": ["bugbounty-mcp.find_subdomains_passive", "find_subdomains_passive"],
    "stub_http_recon": ["bugbounty-mcp.probe_http", "probe_http"],
    "http_recon": ["bugbounty-mcp.probe_http", "probe_http"],
    "stub_endpoint_recon": [],
    "endpoint_recon": [],
}


def _resolve_tool_for_step(step_name: str, tools: ToolRegistry | None) -> str | None:
    if tools is None:
        return None
    if tools.get(step_name) is not None:
        return step_name
    for candidate in RECON_TOOL_MAP.get(step_name, []):
        if tools.get(candidate) is not None:
            return candidate
    if tools.get(f"bugbounty-mcp.{step_name}") is not None:
        return f"bugbounty-mcp.{step_name}"
    candidates = RECON_TOOL_MAP.get(step_name, [])
    base_names = [c.split(".")[-1] for c in candidates]
    if hasattr(tools, "list"):
        for spec in tools.list():
            for base in base_names:
                if spec.name == base or spec.name.endswith(f".{base}"):
                    return spec.name
    return None


def _parse_tool_result(raw: Any) -> Any:
    if isinstance(raw, dict):
        if "content" in raw and isinstance(raw["content"], list):
            for item in raw["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    try:
                        return json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        return text
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


async def stub_subdomain_recon(target: str, context: dict) -> dict:
    """Passive subdomain discovery. Calls real MCP tool if available, falls back to fixture."""
    tools = context.get("tools")
    if tools is None:
        stub = dict(_fixture_subdomain_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub

    tool_name = _resolve_tool_for_step("stub_subdomain_recon", tools)
    if tool_name is None:
        # Real tools configured, but no subdomain tool registered -> no fabrication.
        return {"assets": [], "endpoints": [], "findings": [], "_real_tool": False}

    task_id = context.get("task_id")
    clean_target = normalize_target(target)
    is_ip_or_local = clean_target.replace(".", "").isdigit() or clean_target in ("localhost", "127.0.0.1")
    if is_ip_or_local and tool_name == "bugbounty-mcp.find_subdomains_passive":
        # Private IPs and localhost do not exist in public CT logs (crt.sh).
        # Avoid unsafe external third-party traffic; return empty subdomains.
        return {"assets": [], "endpoints": [], "findings": [], "_real_tool": True}

    args = {"domain": clean_target, "target": clean_target, "targets": [clean_target]}
    try:
        result = await tools.call(tool_name, args, task_id=task_id)
        bus = getattr(tools, "event_bus", None)
        if bus is not None:
            from uap.observability.events import EventKind
            bus.emit_kind(
                EventKind.MCP_CALL,
                task_id=task_id,
                duration_ms=result.duration_ms,
                data={"tool": tool_name, "args": args, "ok": result.ok},
            )
        if not result.ok:
            stub = dict(_fixture_subdomain_recon(target))
            stub["_real_tool"] = False
            stub["_stub_fallback"] = True
            return stub

        data = _parse_tool_result(result.result)
        if isinstance(data, dict) and ("findings" in data or "assets" in data):
            return {
                "assets": data.get("assets", []),
                "endpoints": data.get("endpoints", []),
                "findings": data.get("findings", []),
                "_real_tool": True,
            }

        subdomains: list[str] = []
        if isinstance(data, dict):
            raw_subs = data.get("subdomains") or []
            if isinstance(raw_subs, list):
                subdomains = [str(s).strip() for s in raw_subs if str(s).strip()]
        elif isinstance(data, list):
            subdomains = [str(s).strip() for s in data if str(s).strip()]

        if not subdomains:
            return {"assets": [], "endpoints": [], "findings": [], "_real_tool": True}

        assets = [{"host": sub, "kind": "subdomain"} for sub in subdomains]
        findings = [
            {
                "title": f"Passive subdomain discovery on {sub}",
                "target": sub,
                "evidence": {
                    "detail": f"Discovered subdomain {sub} via {tool_name} for {target}",
                    "status": 200,
                    "response": "crt.sh record",
                },
                "source": tool_name,
            }
            for sub in subdomains
        ]
        return {
            "assets": assets,
            "endpoints": [],
            "findings": findings,
            "_real_tool": True,
        }
    except Exception as exc:
        log.warning("Subdomain tool %s failed for %s: %s; falling back to stub", tool_name, target, exc)
        stub = dict(_fixture_subdomain_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub


async def stub_endpoint_recon(target: str, context: dict) -> dict:
    """Endpoint discovery. Calls real MCP tool if available, falls back to fixture."""
    tools = context.get("tools")
    if tools is None:
        stub = dict(_fixture_endpoint_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub

    tool_name = _resolve_tool_for_step("stub_endpoint_recon", tools)
    if tool_name is None:
        # Real tools configured, but no endpoint tool registered -> no fabrication.
        return {"assets": [], "endpoints": [], "findings": [], "_real_tool": False}

    task_id = context.get("task_id")
    clean_target = normalize_target(target)
    args = {"target": clean_target, "targets": [clean_target]}
    try:
        result = await tools.call(tool_name, args, task_id=task_id)
        bus = getattr(tools, "event_bus", None)
        if bus is not None:
            from uap.observability.events import EventKind
            bus.emit_kind(
                EventKind.MCP_CALL,
                task_id=task_id,
                duration_ms=result.duration_ms,
                data={"tool": tool_name, "args": args, "ok": result.ok},
            )
        if not result.ok:
            stub = dict(_fixture_endpoint_recon(target))
            stub["_real_tool"] = False
            stub["_stub_fallback"] = True
            return stub
        data = _parse_tool_result(result.result)
        if isinstance(data, dict):
            return {
                "assets": data.get("assets", []),
                "endpoints": data.get("endpoints", []),
                "findings": data.get("findings", []),
                "_real_tool": True,
            }
        return {"assets": [], "endpoints": [], "findings": [], "_real_tool": True}
    except Exception as exc:
        log.warning("Endpoint tool %s failed for %s: %s; falling back to stub", tool_name, target, exc)
        stub = dict(_fixture_endpoint_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub


async def stub_http_recon(target: str, context: dict) -> dict:
    """Active HTTP probing. Calls real MCP probe_http if available, falls back to fixture."""
    tools = context.get("tools")
    if tools is None:
        stub = dict(_fixture_http_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub

    tool_name = _resolve_tool_for_step("stub_http_recon", tools)
    if tool_name is None:
        # Real tools configured, but no probe_http tool registered -> no fabrication.
        return {"assets": [], "endpoints": [], "findings": [], "_real_tool": False}

    task_id = context.get("task_id")
    clean_host = normalize_target(target)
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    elif clean_host.startswith("127.0.0.1") or clean_host.startswith("localhost"):
        url = f"http://{target}"
    else:
        url = f"https://{target}"

    args = {"url": url, "target": clean_host, "targets": [clean_host]}
    try:
        result = await tools.call(tool_name, args, task_id=task_id)
        bus = getattr(tools, "event_bus", None)
        if bus is not None:
            from uap.observability.events import EventKind
            bus.emit_kind(
                EventKind.MCP_CALL,
                task_id=task_id,
                duration_ms=result.duration_ms,
                data={"tool": tool_name, "args": args, "ok": result.ok},
            )
        if not result.ok:
            stub = dict(_fixture_http_recon(target))
            stub["_real_tool"] = False
            stub["_stub_fallback"] = True
            return stub

        data = _parse_tool_result(result.result)
        if isinstance(data, dict) and ("findings" in data or "endpoints" in data):
            return {
                "assets": data.get("assets", [{"host": clean_host, "kind": "http_service"}]),
                "endpoints": data.get("endpoints", [{"url": url, "method": "GET"}]),
                "findings": data.get("findings", []),
                "_real_tool": True,
            }

        if not isinstance(data, dict):
            data = {}

        status_code = data.get("status_code")
        if not status_code:
            return {"assets": [], "endpoints": [], "findings": [], "_real_tool": True}

        final_url = str(data.get("final_url") or url)
        headers = data.get("headers") or {}
        title = data.get("title") or ""

        assets = [{"host": clean_host, "kind": "http_service"}]
        endpoints = [{"url": final_url, "method": "GET"}]
        findings = []

        missing_headers = [
            h for h in ("Content-Security-Policy", "Strict-Transport-Security", "X-Frame-Options")
            if h not in headers
        ]
        for mh in missing_headers:
            findings.append(
                {
                    "title": f"Missing security header {mh} on {clean_host}",
                    "target": clean_host,
                    "evidence": {
                        "detail": f"{mh} header is absent from HTTP response",
                        "status": status_code,
                        "response": f"HTTP {status_code} - headers: {list(headers.keys())}",
                    },
                    "source": tool_name,
                }
            )

        if not findings:
            findings.append(
                {
                    "title": f"Active HTTP service on {clean_host} ({status_code})",
                    "target": clean_host,
                    "evidence": {
                        "detail": f"HTTP probe returned status {status_code}, title: {title}",
                        "status": status_code,
                        "response": f"HTTP {status_code}",
                    },
                    "source": tool_name,
                }
            )

        return {
            "assets": assets,
            "endpoints": endpoints,
            "findings": findings,
            "_real_tool": True,
        }
    except Exception as exc:
        log.warning("HTTP probe tool %s failed for %s: %s; falling back to stub", tool_name, target, exc)
        stub = dict(_fixture_http_recon(target))
        stub["_real_tool"] = False
        stub["_stub_fallback"] = True
        return stub


DEFAULT_RECON_STEPS: tuple[ReconStep, ...] = (
    stub_subdomain_recon,
    stub_endpoint_recon,
    stub_http_recon,
)
# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _step_name(step: ReconStep) -> str:
    name = getattr(step, "__name__", None)
    return str(name) if name else type(step).__name__


def _stamp(item: Any, target: str, source: str) -> dict:
    record = dict(item) if isinstance(item, dict) else {"value": item}
    record.setdefault("target", target)
    record.setdefault("source", source)
    return record


def _stamp_finding(finding: Any, target: str, source: str) -> Finding:
    record = dict(finding) if isinstance(finding, dict) else {"title": str(finding)}
    record.setdefault("target", target)
    record["recon_source"] = source
    return record


def _targets_from(state: WorkflowState) -> list[str]:
    task = state.data.get("task") or {}
    for container in (task.get("input") or {}, task.get("constraints") or {}):
        value = container.get("targets")
        if value:
            if isinstance(value, (list, tuple)):
                return [str(item) for item in value]
            return [str(value)]
    return []


def _validates(finding: Finding) -> bool:
    """A finding validates only with a target, evidence, and provenance or a marker."""
    target = str(finding.get("target", "")).strip()
    evidence = finding.get("evidence")
    if not target or not evidence:
        return False
    if finding.get("source") or finding.get("recon_source"):
        return True
    if isinstance(evidence, dict):
        return any(
            key in evidence for key in ("status", "http_status", "response", "reproducible")
        )
    return False


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #

class BBPWorkflow:
    """Master section 9 security pipeline, driven by `WorkflowRunner`."""

    def __init__(
        self,
        scope_gate: ScopeGate | None = None,
        recon_steps: list[ReconStep] | None = None,
        tools: "ToolRegistry | None" = None,
        state_store: StateStore | None = None,
        max_retries: int = 2,
    ) -> None:
        self.scope_gate = scope_gate
        self.recon_steps: tuple[ReconStep, ...] = tuple(
            recon_steps if recon_steps is not None else DEFAULT_RECON_STEPS
        )
        if not self.recon_steps:
            raise ValueError("at least one recon step is required")
        #: True when any recon step is a built-in stub (Master spec section 49:
        #: fabricated findings must be labelled as simulation, never presented
        #: as validated security findings). Real tool-backed steps flip this off.
        self.simulated = any(
            getattr(step, "__name__", "").startswith("stub_")
            for step in self.recon_steps
        )
        self.tools = tools
        self.runner = WorkflowRunner(
            WORKFLOW_NAME,
            nodes={
                "scope_validation": self._scope_validation,
                "recon_planning": self._recon_planning,
                "asset_discovery": self._asset_discovery,
                "endpoint_discovery": self._endpoint_discovery,
                "finding_generation": self._finding_generation,
                "finding_classification": self._finding_classification,
                "validation": self._validation,
                "report": self._report,
            },
            entry="scope_validation",
            state_store=state_store,
            max_retries=max_retries,
        )

    def graph_spec(self) -> dict[str, Any]:
        """Return the canonical graph shape for this workflow (no execution)."""
        recon_step_names = [_step_name(s) for s in self.recon_steps]
        nodes = [
            {"id": "input", "kind": "input", "title": "User Input",
             "config": {}, "position": [100, 300],
             "inputs": [], "outputs": [{"name": "targets", "type": "json", "required": True}]},
            {"id": "scope_validation", "kind": "condition", "title": "Scope Validation",
             "config": {"gate": "ScopeGate"}, "position": [260, 300],
             "inputs": [{"name": "targets", "type": "json", "required": True}],
             "outputs": [{"name": "targets", "type": "json", "required": True}]},
            {"id": "recon_planning", "kind": "agent", "title": "Recon Planning",
             "config": {"step": "recon_planning", "recon_steps": recon_step_names},
             "position": [420, 300],
             "inputs": [{"name": "targets", "type": "json", "required": True}],
             "outputs": [{"name": "plan", "type": "json", "required": True}]},
            {"id": "asset_discovery", "kind": "tool", "title": "Asset Discovery",
             "config": {"step": "asset_discovery", "recon_steps": recon_step_names},
             "position": [580, 300],
             "inputs": [{"name": "plan", "type": "json", "required": True}],
             "outputs": [{"name": "assets", "type": "json", "required": True}]},
            {"id": "endpoint_discovery", "kind": "tool", "title": "Endpoint Discovery",
             "config": {"step": "endpoint_discovery"}, "position": [740, 300],
             "inputs": [{"name": "assets", "type": "json", "required": True}],
             "outputs": [{"name": "endpoints", "type": "json", "required": True}]},
            {"id": "finding_generation", "kind": "agent", "title": "Finding Generation",
             "config": {"step": "finding_generation"}, "position": [900, 300],
             "inputs": [{"name": "endpoints", "type": "json", "required": True}],
             "outputs": [{"name": "findings", "type": "json", "required": True}]},
            {"id": "finding_classification", "kind": "agent", "title": "Finding Classification",
             "config": {"step": "finding_classification", "classifier": "classify_severity"},
             "position": [1060, 300],
             "inputs": [{"name": "findings", "type": "json", "required": True}],
             "outputs": [{"name": "classified", "type": "json", "required": True}]},
            {"id": "validation", "kind": "evaluation", "title": "Validation",
             "config": {"step": "validation", "verifier": "bbp_report"},
             "position": [1220, 300],
             "inputs": [{"name": "classified", "type": "json", "required": True}],
             "outputs": [{"name": "validated_findings", "type": "json", "required": True}]},
            {"id": "report", "kind": "synthesis", "title": "Report",
             "config": {"step": "report"}, "position": [1380, 300],
             "inputs": [{"name": "validated_findings", "type": "json", "required": True}],
             "outputs": [{"name": "report", "type": "text", "required": True}]},
            {"id": "output", "kind": "output", "title": "Result",
             "config": {}, "position": [1540, 300],
             "inputs": [{"name": "report", "type": "text", "required": True}],
             "outputs": []},
        ]
        node_ids = [n["id"] for n in nodes]
        edges = []
        for i in range(len(node_ids) - 1):
            src = nodes[i]
            tgt = nodes[i + 1]
            src_port = src["outputs"][0]["name"] if src["outputs"] else "out"
            tgt_port = tgt["inputs"][0]["name"] if tgt["inputs"] else "in"
            edges.append({
                "id": f"e{i+1}", "source": node_ids[i], "source_port": src_port,
                "target": node_ids[i + 1], "target_port": tgt_port, "kind": "data",
            })
        return {
            "id": f"wf-{WORKFLOW_NAME}",
            "name": WORKFLOW_NAME,
            "description": "Bug bounty pipeline: scope-gated recon through validated findings",
            "nodes": nodes,
            "edges": edges,
        }

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def run(self, task: TaskSpec) -> WorkflowResult:
        return await self.runner.run(task)

    async def resume(self, task_id: str) -> WorkflowResult:
        return await self.runner.resume(task_id)

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #

    async def _scope_validation(self, state: WorkflowState) -> NodeResult:
        """Hard gate: policy decides before any probe (Master section 9)."""
        if self.scope_gate is None:
            raise RuntimeError("no scope gate configured: refusing to run")

        targets = _targets_from(state)
        allowed, blocked = self.scope_gate.filter(targets)
        blocked_targets = self.scope_gate.blocked_reasons(blocked)
        scope = {
            "in_scope": list(self.scope_gate.in_scope),
            "out_of_scope": list(self.scope_gate.out_of_scope),
        }

        if not allowed:
            notes = (
                f"blocked: no in-scope targets remain ({len(blocked_targets)} "
                f"target(s) excluded)"
            )
            verdict = VerificationResult(
                verifier="bbp_scope_gate",
                passed=False,
                notes=notes,
            )
            return NodeResult(
                {
                    "targets": [],
                    "blocked_targets": blocked_targets,
                    "scope": scope,
                    "scope_gate_ran": True,
                    "verification": verdict,
                },
                next_node=None,
            )

        return NodeResult(
            {
                "targets": allowed,
                "blocked_targets": blocked_targets,
                "scope": scope,
                "scope_gate_ran": True,
            },
            next_node="recon_planning",
        )

    async def _recon_planning(self, state: WorkflowState) -> NodeResult:
        targets = state.data.get("targets", [])
        steps = [_step_name(step) for step in self.recon_steps]
        plan: dict[str, Any] = {
            "targets": list(targets),
            "steps": steps,
            "strategy": "sequential",
            "skipped_steps": [],
            "tool_results": {},
        }

        denials: list[dict[str, Any]] = []
        pending_approvals: list[dict[str, Any]] = []
        if self.tools is not None:
            for step in self.recon_steps:
                name = _step_name(step)
                tool_name = _resolve_tool_for_step(name, self.tools)
                if tool_name is None:
                    continue

                entry = self.tools.get(tool_name)
                if entry is None:
                    continue
                spec, _ = entry
                decision = self.tools.policy.check(spec, None)
                if not decision.allowed:
                    eff_task_id = state.task_id or "default"
                    call_result = await self.tools.call(
                        tool_name, {"targets": list(targets)}, task_id=eff_task_id
                    )
                    plan["skipped_steps"].append(name)
                    denial_entry: dict[str, Any] = {"tool": name, "error": call_result.error or decision.reason}
                    if call_result.approval_id:
                        denial_entry["approval_id"] = call_result.approval_id
                        denial_entry["needs_approval"] = True
                        pending_approvals.append(
                            {
                                "approval_id": call_result.approval_id,
                                "tool": name,
                                "error": call_result.error or decision.reason,
                            }
                        )
                    denials.append(denial_entry)
        updates: dict[str, Any] = {"plan": plan, "tool_denials": denials}
        if pending_approvals:
            updates["pending_approvals"] = pending_approvals
        return NodeResult(updates, next_node="asset_discovery")

    async def _asset_discovery(self, state: WorkflowState) -> NodeResult:
        """Run the (allowed) recon steps; denied steps are skipped, not crashed."""
        targets = state.data.get("targets", [])
        plan = state.data.get("plan", {})
        skipped = set(plan.get("skipped_steps", []))
        assets: list[dict] = []
        raw_endpoints: list[dict] = []
        raw_findings: list[Finding] = []
        real_tools_ran = False
        stub_used = False

        for target in targets:
            context = {
                "target": target,
                "plan": plan,
                "tools": self.tools,
                "task_id": state.task_id,
            }
            for step in self.recon_steps:
                name = _step_name(step)
                if name in skipped:
                    continue
                payload = await step(target, context) or {}
                if not isinstance(payload, dict):
                    continue
                if payload.get("_real_tool"):
                    real_tools_ran = True
                if payload.get("_stub_fallback"):
                    stub_used = True
                for asset in payload.get("assets", []) or []:
                    assets.append(_stamp(asset, target, name))
                for endpoint in payload.get("endpoints", []) or []:
                    raw_endpoints.append(_stamp(endpoint, target, name))
                for finding in payload.get("findings", []) or []:
                    raw_findings.append(_stamp_finding(finding, target, name))

        if real_tools_ran and not stub_used:
            simulated = False
        elif stub_used:
            simulated = True
        else:
            simulated = self.simulated
        self.simulated = simulated

        return NodeResult(
            {
                "assets": assets,
                "raw_endpoints": raw_endpoints,
                "raw_findings": raw_findings,
                "simulated": simulated,
            },
            next_node="endpoint_discovery",
        )

    async def _endpoint_discovery(self, state: WorkflowState) -> NodeResult:
        endpoints: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in state.data.get("raw_endpoints", []):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            key = (url, str(item.get("method", "GET")))
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(
                {
                    "url": url,
                    "method": str(item.get("method", "GET")),
                    "target": str(item.get("target", "")),
                    "source": str(item.get("source", "")),
                }
            )
        return NodeResult({"endpoints": endpoints}, next_node="finding_generation")

    async def _finding_generation(self, state: WorkflowState) -> NodeResult:
        findings = [dict(item) for item in state.data.get("raw_findings", [])]
        # Everything is unvalidated until the validation node says otherwise.
        return NodeResult(
            {"findings": findings, "unvalidated": list(findings)},
            next_node="finding_classification",
        )

    async def _finding_classification(self, state: WorkflowState) -> NodeResult:
        classified = []
        for finding in state.data.get("findings", []):
            record = dict(finding)
            record["severity"] = classify_severity(record)
            classified.append(record)
        return NodeResult({"classified": classified}, next_node="validation")

    async def _validation(self, state: WorkflowState) -> NodeResult:
        validated: list[Finding] = []
        unvalidated: list[Finding] = []
        for finding in state.data.get("classified", []):
            record = dict(finding)
            if _validates(record):
                record["validated"] = True
                validated.append(record)
            else:
                record["validated"] = False
                unvalidated.append(record)
        return NodeResult(
            {"validated_findings": validated, "unvalidated": unvalidated},
            next_node="report",
        )

    async def _report(self, state: WorkflowState) -> NodeResult:
        validated = state.data.get("validated_findings", [])
        unvalidated = state.data.get("unvalidated", [])
        simulated = state.data.get("simulated", self.simulated)
        report = _build_report(state, validated, unvalidated, simulated=simulated)
        if simulated:
            # Section 49: the machine-readable artifact must carry the same
            # warning as the report, so downstream consumers cannot mistake
            # stub findings for real ones.
            findings_payload: Any = {
                "_SIMULATION": "deterministic stub recon — fixed sample data, not real security findings",
                "findings": validated,
            }
        else:
            findings_payload = validated
        findings_json = json.dumps(findings_payload, indent=2, sort_keys=True, default=str)

        task_id = state.task_id
        artifacts = [
            Artifact(
                task_id=task_id,
                type="findings.json",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=findings_json,
            ),
            Artifact(
                task_id=task_id,
                type="report.md",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=report,
            ),
        ]

        verification = self._verify(state, validated, report)
        return NodeResult(
            {
                "report": report,
                "output": report,
                "artifacts": artifacts,
                "verification": verification,
            },
            next_node=None,
        )

    # ------------------------------------------------------------------ #
    # Verification (Master section 17)
    # ------------------------------------------------------------------ #

    def _verify(
        self, state: WorkflowState, validated: list[Finding], report: str
    ) -> VerificationResult:
        def scope_ran(subject: dict) -> bool:
            return bool(subject.get("scope_gate_ran"))

        def evidence_ok(subject: dict) -> bool:
            return all(
                str(item.get("target", "")).strip() and item.get("evidence")
                for item in subject.get("validated_findings", [])
            )

        def report_ok(subject: dict) -> bool:
            return bool(str(subject.get("report", "")).strip())

        criteria = [
            Criterion(
                name="scope_gate_ran",
                check=scope_ran,
                evidence=lambda s: f"scope_gate_ran={bool(s.get('scope_gate_ran'))}",
            ),
            Criterion(
                name="all_findings_have_evidence",
                check=evidence_ok,
                evidence=lambda s: (
                    f"{len(s.get('validated_findings', []))} validated finding(s) "
                    "each carry target + evidence"
                ),
            ),
            Criterion(
                name="report_non_empty",
                check=report_ok,
                evidence=lambda s: f"report length {len(str(s.get('report', '')).strip())}",
            ),
        ]
        verifier = DeterministicVerifier("bbp_report", criteria)
        return verifier.verify(
            {
                "scope_gate_ran": state.data.get("scope_gate_ran", False),
                "validated_findings": validated,
                "report": report,
            }
        )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _render_evidence(evidence: Any) -> str:
    if isinstance(evidence, dict):
        detail = str(evidence.get("detail", "")).strip()
        marker = ""
        if evidence.get("status") is not None:
            marker = f" [status {evidence['status']}]"
        elif evidence.get("response"):
            marker = f" [response {str(evidence['response'])[:80]}]"
        return (detail or "no detail") + marker
    return str(evidence)


def _build_report(
    state: WorkflowState,
    validated: list[Finding],
    unvalidated: list[Finding],
    *,
    simulated: bool = False,
) -> str:
    scope = state.data.get("scope", {})
    targets = state.data.get("targets", [])
    blocked = state.data.get("blocked_targets", [])

    lines = ["# BBP Report", ""]
    if simulated:
        # Master spec section 49: stub recon must never be presented as real
        # security findings. Say it at the top, in the artifact itself.
        lines.extend(
            [
                "> **SIMULATION — DETERMINISTIC STUB RECON**",
                "> This run used built-in deterministic stub recon steps. The assets,",
                "> endpoints, and findings below are FIXED SAMPLE DATA, not real",
                "> security testing. Do not act on them or report them to any program.",
                "",
            ]
        )
    lines.extend(["## Scope", ""])
    in_scope = scope.get("in_scope", [])
    out_of_scope = scope.get("out_of_scope", [])
    lines.append(f"- in-scope: {', '.join(in_scope) if in_scope else 'none'}")
    lines.append(
        f"- out-of-scope: {', '.join(out_of_scope) if out_of_scope else 'none'}"
    )

    lines.extend(["", "## Targets", ""])
    if targets:
        lines.extend(f"- {target}" for target in targets)
    else:
        lines.append("- none")

    lines.extend(["", "## Blocked", ""])
    if blocked:
        for record in blocked:
            lines.append(f"- {record.get('target', '')} — {record.get('reason', '')}")
    else:
        lines.append("- none")

    lines.extend(["", "## Validated findings", ""])
    if validated:
        for finding in validated:
            severity = str(finding.get("severity", "info")).upper()
            title = str(finding.get("title", finding.get("name", "untitled")))
            lines.append(f"- [{severity}] {title} (target: {finding.get('target', '')})")
            lines.append(f"  - evidence: {_render_evidence(finding.get('evidence'))}")
    else:
        lines.append("- none")

    lines.extend(["", "## Unvalidated (appendix)", ""])
    if unvalidated:
        for finding in unvalidated:
            title = str(finding.get("title", finding.get("name", "untitled")))
            lines.append(
                f"- {title} (target: {finding.get('target', '')}) — "
                "excluded: missing target/evidence"
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            f"{len(validated)} validated finding(s), "
            f"{len(unvalidated)} unvalidated, {len(blocked)} blocked target(s).",
            "",
        ]
    )
    return "\n".join(lines)
