"""Tests for the proposal endpoint and workflow graph specs."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app
from uap.workflows.bbp import BBPWorkflow
from uap.workflows.research import ResearchWorkflow


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    with TestClient(app) as c:
        yield c


# -- Graph spec integrity --------------------------------------------------- #


def test_research_graph_nodes_match_runner():
    """Every declared graph node id (minus input/output) exists in runner.nodes."""
    wf = ResearchWorkflow()
    graph = wf.graph_spec()
    runner_nodes = set(wf.runner.nodes.keys())
    graph_node_ids = {n["id"] for n in graph["nodes"]}
    # input/output are orchestration nodes, not runner nodes.
    workflow_ids = graph_node_ids - {"input", "output"}
    missing = workflow_ids - runner_nodes
    assert not missing, f"graph declares nodes not in runner: {missing}"
    # Every runner node must appear in the graph.
    extra = runner_nodes - graph_node_ids
    assert not extra, f"runner has nodes not declared in graph: {extra}"


def test_bbp_graph_nodes_match_runner():
    """Every declared graph node id (minus input/output) exists in runner.nodes."""
    wf = BBPWorkflow()
    graph = wf.graph_spec()
    runner_nodes = set(wf.runner.nodes.keys())
    graph_node_ids = {n["id"] for n in graph["nodes"]}
    workflow_ids = graph_node_ids - {"input", "output"}
    missing = workflow_ids - runner_nodes
    assert not missing, f"graph declares nodes not in runner: {missing}"
    extra = runner_nodes - graph_node_ids
    assert not extra, f"runner has nodes not declared in graph: {extra}"


def test_research_graph_edges_reference_declared_nodes():
    wf = ResearchWorkflow()
    graph = wf.graph_spec()
    node_ids = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in node_ids, f"edge source {edge['source']} not in nodes"
        assert edge["target"] in node_ids, f"edge target {edge['target']} not in nodes"


def test_bbp_graph_edges_reference_declared_nodes():
    wf = BBPWorkflow()
    graph = wf.graph_spec()
    node_ids = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["source"] in node_ids, f"edge source {edge['source']} not in nodes"
        assert edge["target"] in node_ids, f"edge target {edge['target']} not in nodes"


def test_research_graph_has_required_fields():
    graph = ResearchWorkflow().graph_spec()
    assert "id" in graph
    assert "nodes" in graph
    assert "edges" in graph
    for node in graph["nodes"]:
        assert "id" in node
        assert "kind" in node
        assert "config" in node
        assert "position" in node


def test_bbp_graph_has_required_fields():
    graph = BBPWorkflow().graph_spec()
    assert "id" in graph
    assert "nodes" in graph
    assert "edges" in graph
    for node in graph["nodes"]:
        assert "id" in node
        assert "kind" in node
        assert "config" in node
        assert "position" in node


def test_research_graph_config_references_real_collectors():
    wf = ResearchWorkflow()
    graph = wf.graph_spec()
    fan_out = next(n for n in graph["nodes"] if n["id"] == "fan_out")
    collectors = fan_out["config"]["collectors"]
    assert "stub_web_collector" in collectors
    assert "stub_papers_collector" in collectors
    assert "stub_docs_collector" in collectors


def test_research_graph_synthesis_config():
    wf = ResearchWorkflow()  # no synthesizer
    graph = wf.graph_spec()
    synth = next(n for n in graph["nodes"] if n["id"] == "synthesis")
    assert synth["config"]["synthesizer"] == "deterministic"


def test_bbp_graph_config_references_real_recon_steps():
    wf = BBPWorkflow()
    graph = wf.graph_spec()
    asset = next(n for n in graph["nodes"] if n["id"] == "asset_discovery")
    steps = asset["config"]["recon_steps"]
    assert "stub_subdomain_recon" in steps
    assert "stub_endpoint_recon" in steps
    assert "stub_http_recon" in steps


# -- Proposal endpoint ------------------------------------------------------- #


def test_proposal_research(client: TestClient):
    res = client.post("/api/proposals", json={"input": "research quantum computing"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "proposal"
    assert data["domain"] == "research"
    assert "graph" in data
    assert "nodes" in data["graph"]
    assert "edges" in data["graph"]
    assert "resources" in data
    assert "reasoning" in data
    assert isinstance(data["reasoning"], list)


def test_proposal_clarification(client: TestClient):
    # An empty input should trigger clarification.
    res = client.post("/api/proposals", json={"input": ""})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "clarification"
    assert "question" in data


def test_proposal_bbp(client: TestClient):
    res = client.post(
        "/api/proposals",
        json={"input": "bug bounty on example.com"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "proposal"
    assert data["domain"] == "bbp"
    assert data["graph"] is not None


def test_proposal_no_execution_no_db(client: TestClient):
    """Proposal must not create a task."""
    res = client.post("/api/proposals", json={"input": "research AI safety"})
    assert res.status_code == 200
    # No task should exist.
    tasks = client.get("/tasks").json()
    assert len(tasks) == 0


# -- Workflows endpoint ----------------------------------------------------- #


def test_workflows_endpoint(client: TestClient):
    res = client.get("/api/workflows")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 2
    names = {w["name"] for w in data}
    assert "research" in names
    assert "bbp" in names
    for wf in data:
        assert wf["execution_mode"] == "deterministic-stubs"
        assert "graph" in wf
        assert "nodes" in wf["graph"]
