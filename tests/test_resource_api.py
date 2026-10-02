"""Tests for resource, workspace, artifact, and knowledge API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    with TestClient(app) as c:
        yield c


# -- /api/resources/* -------------------------------------------------------- #


def test_resources_agents(client: TestClient):
    res = client.get("/api/resources/agents")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["name"] == "llm"
    assert "capabilities" in data[0]


def test_resources_tools(client: TestClient):
    res = client.get("/api/resources/tools")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    # At minimum the local tools should be present.
    for tool in data:
        assert "name" in tool
        assert "source" in tool
        assert tool["source"] in ("local", "mcp")


def test_resources_skills(client: TestClient):
    res = client.get("/api/resources/skills")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


def test_resources_models(client: TestClient):
    res = client.get("/api/resources/models")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    for model in data:
        assert "id" in model
        assert "provider" in model
        assert "capabilities" in model


def test_resources_policies(client: TestClient):
    res = client.get("/api/resources/policies")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    for policy in data:
        assert "id" in policy
        assert "effect" in policy
        assert "priority" in policy


def test_resources_mcp(client: TestClient):
    res = client.get("/api/resources/mcp")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "bugbounty-mcp"
    assert "tools" in data[0]
    assert "running" in data[0]


# -- /api/workspaces -------------------------------------------------------- #


def test_workspaces_empty(client: TestClient):
    res = client.get("/api/workspaces")
    assert res.status_code == 200
    # PG may be down; that's fine, returns [].
    assert isinstance(res.json(), list)


# -- /api/tasks/{task_id}/artifacts ------------------------------------------ #


def test_artifacts_unknown_task(client: TestClient):
    res = client.get("/api/tasks/nonexistent/artifacts")
    assert res.status_code == 404


def test_artifacts_and_content(client: TestClient):
    task_res = client.post(
        "/tasks", json={"input": "research best practices"}
    ).json()
    task_id = task_res["task_id"]

    res = client.get(f"/api/tasks/{task_id}/artifacts")
    assert res.status_code == 200
    artifacts = res.json()
    assert isinstance(artifacts, list)
    for art in artifacts:
        assert "index" in art
        assert "type" in art
        assert "content_available" in art


def test_artifact_content_bad_index(client: TestClient):
    task_res = client.post(
        "/tasks", json={"input": "research best practices"}
    ).json()
    task_id = task_res["task_id"]

    res = client.get(f"/api/tasks/{task_id}/artifacts/999/content")
    assert res.status_code == 404


# -- /api/knowledge --------------------------------------------------------- #


def test_knowledge_empty(client: TestClient):
    res = client.get("/api/knowledge")
    assert res.status_code == 200
    # PG may be down; returns [].
    assert isinstance(res.json(), list)


def test_knowledge_provenance_unknown(client: TestClient):
    res = client.get("/api/knowledge/00000000-0000-0000-0000-000000000000/provenance")
    assert res.status_code == 404


# -- POST /tasks honesty metadata -------------------------------------------- #


def test_task_response_has_honesty_metadata(client: TestClient):
    """The response must declare what actually produced the evidence.

    This used to assert the literal "deterministic-stubs". That value was
    hardcoded and never updated, so a run that really queried crt.sh through
    bugbounty-mcp still told the user its findings were fixtures (found
    2026-10-03). The property that matters is that the field is present and
    tells the truth about the run — not that it holds one frozen string.
    """
    res = client.post("/tasks", json={"input": "research AI safety"}).json()
    source = res["evidence_source"]
    assert isinstance(source, str) and source
    # One of the honest states: MCP was used, MCP was available, or it was
    # genuinely a stub run. Never an empty or unknown label.
    assert source.startswith(("mcp", "deterministic-stubs")), source


def test_task_response_echoes_workspace_id(client: TestClient):
    res = client.post(
        "/tasks", json={"input": "research testing", "workspace_id": "ws-42"}
    ).json()
    assert res.get("workspace_id") == "ws-42"


# -- Execution graph -------------------------------------------------------- #


def test_execution_graph_real_nodes(client: TestClient):
    """The execution graph endpoint returns real workflow nodes, not a placeholder."""
    task_res = client.post(
        "/tasks", json={"input": "research best practices"}
    ).json()
    task_id = task_res["task_id"]

    res = client.get(f"/api/executions/{task_id}/graph")
    assert res.status_code == 200
    graph = res.json()
    node_ids = {n["id"] for n in graph["nodes"]}
    # Must contain real research nodes, not the old "Agent Core" placeholder.
    assert "question_analysis" in node_ids
    assert "synthesis" in node_ids
    assert "review" in node_ids
    # Must never contain placeholder nodes.
    titles = {n.get("title") for n in graph["nodes"]}
    assert "Agent Core" not in titles
    # Must include execution status.
    assert "execution" in graph
    assert "status" in graph["execution"]
    # Must include per-node status.
    for node in graph["nodes"]:
        assert "status" in node


def test_execution_graph_unknown_404(client: TestClient):
    res = client.get("/api/executions/nonexistent/graph")
    assert res.status_code == 404
    assert res.json()["detail"] == "unknown execution"
