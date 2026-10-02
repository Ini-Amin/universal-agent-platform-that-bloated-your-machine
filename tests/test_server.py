"""Tests for the SSE-first FastAPI server (build Step 14).

Uses ``fastapi.testclient.TestClient`` against ``create_app(run_inline=True)`` so
every workflow completes synchronously inside the request -- no sleeps, no
background-task races, fully deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.observability import EventBus, EventKind
from uap.server import create_app

RESEARCH_INPUT = "research the best langgraph checkpointer approach"

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def app(tmp_path: Path):
    return create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        heartbeat_interval=0.05,
    )

@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client

def _submit(client: TestClient, text: str = RESEARCH_INPUT) -> dict:
    response = client.post("/tasks", json={"input": text, "user_id": "tester"})
    assert response.status_code == 200
    return response.json()

# --------------------------------------------------------------------------- #
# 1. Static UI
# --------------------------------------------------------------------------- #

def test_root_serves_canvas_ide(client: TestClient):
    """The canvas IDE is the primary UI at / (old UI moved to /legacy/)."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "UAP" in response.text
    # Product identity (Master spec section 1): a visual operating environment,
    # not a generic canvas demo.
    assert "Visual Operating Environment" in response.text


def test_legacy_ui_still_served(client: TestClient):
    response = client.get("/legacy/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Universal Agent Platform" in response.text

# --------------------------------------------------------------------------- #
# 2. Accepted research task completes
# --------------------------------------------------------------------------- #

def test_research_task_accepted_then_completes(client: TestClient):
    payload = _submit(client)
    assert payload["status"] == "accepted"
    assert payload["domain"] == "research"
    assert payload["workflow"] == "ResearchWorkflow"
    task_id = payload["task_id"]

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["status"] == "completed"

def test_background_task_polling_completes(tmp_path: Path):
    """Non-inline mode: the run is dispatched via asyncio.create_task."""
    import time

    app = create_app(runs_dir=tmp_path / "runs", heartbeat_interval=0.05)
    with TestClient(app) as client:
        payload = _submit(client)
        assert payload["status"] == "accepted"

        deadline = time.monotonic() + 5.0
        detail = {"status": "accepted"}
        while time.monotonic() < deadline:
            detail = client.get(f"/tasks/{payload['task_id']}").json()
            if detail["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)

    assert detail["status"] == "completed"
    assert detail["output"].strip()

# --------------------------------------------------------------------------- #
# 3. Output + artifacts
# --------------------------------------------------------------------------- #

def test_completed_run_has_output_and_expected_artifacts(client: TestClient):
    task_id = _submit(client)["task_id"]
    detail = client.get(f"/tasks/{task_id}").json()

    assert detail["output"].strip()
    # The durable slice produces per-pipeline-node artifacts named
    # slice-<node> (question_analysis, fan_out, synthesis, review, ...).
    types = [a["type"] for a in detail["artifacts"]]
    assert types, "expected at least one artifact"
    assert any(
        "synthesis" in t or t.startswith("slice-") or t in {"report.md", "sources.json"}
        for t in types
    )
    assert all(a["uri"] for a in detail["artifacts"])

# --------------------------------------------------------------------------- #
# 4. Empty input -> clarification
# --------------------------------------------------------------------------- #

def test_empty_input_returns_clarification(client: TestClient):
    response = client.post("/tasks", json={"input": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert body["question"]

# --------------------------------------------------------------------------- #
# 5. Listing runs
# --------------------------------------------------------------------------- #

def test_list_tasks_includes_submitted_run(client: TestClient):
    task_id = _submit(client)["task_id"]
    runs = client.get("/tasks").json()
    assert isinstance(runs, list)
    assert any(run["task_id"] == task_id for run in runs)
    assert {"task_id", "domain", "workflow", "status"} <= set(runs[0])

# --------------------------------------------------------------------------- #
# 6. Unknown task -> 404
# --------------------------------------------------------------------------- #

def test_unknown_task_returns_404(client: TestClient):
    assert client.get("/tasks/does-not-exist").status_code == 404

# --------------------------------------------------------------------------- #
# 7. SSE stream
# --------------------------------------------------------------------------- #

def test_events_stream_emits_started_and_finished(client: TestClient):
    task_id = _submit(client)["task_id"]

    with client.stream("GET", f"/events?task_id={task_id}") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        kinds = []
        for line in stream.iter_lines():
            if line.startswith("data:"):
                kinds.append(json.loads(line[len("data:"):].strip())["kind"])

    assert EventKind.TASK_STARTED in kinds
    assert EventKind.TASK_FINISHED in kinds

def test_events_stream_closes_after_finished(client: TestClient):
    """The generator must terminate rather than hang the client forever."""
    task_id = _submit(client)["task_id"]
    with client.stream("GET", f"/events?task_id={task_id}") as stream:
        events = [json.loads(line[5:].strip())
                  for line in stream.iter_lines() if line.startswith("data:")]
    assert events[-1]["kind"] == EventKind.TASK_FINISHED
    assert events[-1]["task_id"] == task_id

# --------------------------------------------------------------------------- #
# 8. Approval flow
# --------------------------------------------------------------------------- #

def test_approval_decide_then_conflict_then_unknown(app, client: TestClient):
    request = app.state.gate.request("task-1", "publish_report", {"scope": "public"})

    first = client.post(
        f"/approvals/{request.approval_id}/decide",
        json={"approved": True, "decided_by": "alice"},
    )
    assert first.status_code == 200
    assert first.json()["state"] == "approved"
    assert first.json()["decided_by"] == "alice"

    second = client.post(
        f"/approvals/{request.approval_id}/decide",
        json={"approved": False, "decided_by": "bob"},
    )
    assert second.status_code == 409

    unknown = client.post(
        "/approvals/nope/decide",
        json={"approved": True, "decided_by": "bob"},
    )
    assert unknown.status_code == 404

# --------------------------------------------------------------------------- #
# 9. Static page wiring
# --------------------------------------------------------------------------- #

def test_static_page_contains_eventsource_and_fetch(client: TestClient):
    html = client.get("/legacy/").text
    assert "EventSource(" in html
    assert 'fetch("/tasks"' in html or "fetch('/tasks'" in html
    assert "/events?task_id=" in html

# --------------------------------------------------------------------------- #
# 10. Node history
# --------------------------------------------------------------------------- #

def test_run_detail_has_eight_research_nodes(client: TestClient):
    task_id = _submit(client)["task_id"]
    detail = client.get(f"/tasks/{task_id}").json()
    # The durable slice runs the REAL research pipeline through the canonical
    # graph: the 8 pipeline nodes bracketed by input/output orchestration.
    assert detail["node_history"] == [
        "input",
        "question_analysis",
        "research_planning",
        "fan_out",
        "evidence_extraction",
        "evidence_filtering",
        "cross_verification",
        "synthesis",
        "review",
        "output",
    ]

# --------------------------------------------------------------------------- #
# 11. Isolation between runs
# --------------------------------------------------------------------------- #

def test_two_sequential_tasks_are_isolated(client: TestClient):
    first = _submit(client, RESEARCH_INPUT)
    second = _submit(client, "research vector database tradeoffs for RAG")

    assert first["task_id"] != second["task_id"]

    detail_a = client.get(f"/tasks/{first['task_id']}").json()
    detail_b = client.get(f"/tasks/{second['task_id']}").json()
    assert detail_a["status"] == detail_b["status"] == "completed"
    assert detail_a["output"] != detail_b["output"]

    listed = {run["task_id"] for run in client.get("/tasks").json()}
    assert {first["task_id"], second["task_id"]} <= listed

# --------------------------------------------------------------------------- #
# 12. Custom bus + runs_dir are honoured
# --------------------------------------------------------------------------- #

def test_create_app_uses_injected_bus_and_runs_dir(tmp_path: Path):
    runs_dir = tmp_path / "custom-runs"
    bus = EventBus()
    app = create_app(bus=bus, runs_dir=runs_dir, run_inline=True)
    client = TestClient(app)

    task_id = _submit(client)["task_id"]
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"

    # The app reused the injected bus and wrote through its sinks.
    assert app.state.bus is bus
    jsonl = runs_dir / "events.jsonl"
    assert jsonl.exists()
    lines = [json.loads(line) for line in jsonl.read_text().splitlines() if line]
    assert any(line["kind"] == EventKind.TASK_FINISHED for line in lines)

    # Artifacts landed under the custom runs dir too (the durable slice writes
    # per-node artifacts: slice-<node>__v1__... plus their .meta.json sidecars).
    task_dir = runs_dir / "artifacts" / task_id
    assert task_dir.is_dir(), f"expected artifacts under {task_dir}"
    files = [p.name for p in task_dir.iterdir()]
    assert any("synthesis" in name for name in files), files
    assert any(name.endswith(".meta.json") for name in files), files

# --------------------------------------------------------------------------- #
# 13. BBP dispatch: registry-based routing, per-task scope gate
# --------------------------------------------------------------------------- #

BBP_INPUT = "Bug bounty on example.com"

#: The exact clarification shown for a domain the server cannot execute yet.
UNAVAILABLE_EXAMPLE = (
    'Try a research request ("research ...") or a bug bounty request '
    '("bug bounty on example.com").'
)

def _submit_bbp(client: TestClient, text: str = BBP_INPUT) -> dict:
    response = client.post("/tasks", json={"input": text, "user_id": "tester"})
    assert response.status_code == 200
    return response.json()

def test_bbp_task_accepted_then_completes_with_bbp_artifacts(
    app, client: TestClient
):
    payload = _submit_bbp(client)
    assert payload["status"] == "accepted"
    assert payload["domain"] == "bbp"
    assert payload["workflow"] == "BBPWorkflow"
    task_id = payload["task_id"]

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    assert sorted(a["type"] for a in detail["artifacts"]) == [
        "findings.json",
        "report.md",
    ]

    runs_dir = app.state.runs_dir
    assert (runs_dir / "artifacts" / task_id / "findings.json").exists()
    assert (runs_dir / "artifacts" / task_id / "report.md").exists()

def test_bbp_screenshot_style_request_completes(client: TestClient):
    text = (
        "Always prioritize authorization, scope compliance, reproducibility, "
        "safety, and evidence. Target: api.example.com"
    )
    payload = _submit_bbp(client, text)
    assert payload["domain"] == "bbp"

    detail = client.get(f"/tasks/{payload['task_id']}").json()
    assert detail["status"] == "completed"
    assert "findings.json" in [a["type"] for a in detail["artifacts"]]

def test_bbp_scope_declaration_flows_through(client: TestClient):
    payload = _submit_bbp(client, "Bug bounty on example.com. In scope: *.example.com")
    detail = client.get(f"/tasks/{payload['task_id']}").json()
    assert detail["status"] == "completed"
    assert detail["node_history"] == [
        "scope_validation",
        "recon_planning",
        "asset_discovery",
        "endpoint_discovery",
        "finding_generation",
        "finding_classification",
        "validation",
        "report",
    ]

def test_bbp_out_of_scope_target_fails_closed(client: TestClient):
    text = (
        "Bug bounty on legacy.example.com. In scope: *.example.com. "
        "Out of scope: legacy.example.com"
    )
    payload = _submit_bbp(client, text)
    detail = client.get(f"/tasks/{payload['task_id']}").json()

    assert detail["status"] == "failed"
    assert "blocked" in (detail["error"] or "")
    # Fail closed: no validated findings artifact is produced for a blocked run.
    assert detail["artifacts"] == []

def test_bbp_without_target_returns_clarification_and_creates_no_run(
    client: TestClient,
):
    response = client.post("/tasks", json={"input": "I want to do a bug bounty"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert body["question"]
    assert "task_id" not in body
    assert client.get("/tasks").json() == []

def test_non_executable_domain_uses_generic_example(client: TestClient):
    response = client.post(
        "/tasks", json={"input": "write a python function to sort a list"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert body["question"] == (
        "The 'coding' domain is not available yet. " + UNAVAILABLE_EXAMPLE
    )
    assert UNAVAILABLE_EXAMPLE in body["question"]

def test_research_still_works_after_refactor(client: TestClient):
    task_id = _submit(client)["task_id"]
    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    assert detail["workflow"] == "ResearchWorkflow"
    # Durable slice artifacts are per-node (slice-recon, slice-synthesize, ...).
    types = [a["type"] for a in detail["artifacts"]]
    assert types, "expected artifacts from the durable slice"
    assert any(t.startswith("slice-") or t in {"report.md", "sources.json"} for t in types)

def test_malformed_scope_pattern_returns_clarification_not_500(
    client: TestClient,
):
    response = client.post(
        "/tasks",
        json={"input": "Bug bounty on example.com. In scope: http://bad.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert "scope" in body["question"].lower()
    assert "task_id" not in body
    assert client.get("/tasks").json() == []

def test_list_tasks_reports_research_and_bbp_independently(client: TestClient):
    research = _submit(client, RESEARCH_INPUT)
    bbp = _submit_bbp(client)

    runs = {run["task_id"]: run for run in client.get("/tasks").json()}
    assert runs[research["task_id"]]["domain"] == "research"
    assert runs[research["task_id"]]["workflow"] == "ResearchWorkflow"
    assert runs[bbp["task_id"]]["domain"] == "bbp"
    assert runs[bbp["task_id"]]["workflow"] == "BBPWorkflow"
    assert runs[research["task_id"]]["status"] == "completed"
    assert runs[bbp["task_id"]]["status"] == "completed"
