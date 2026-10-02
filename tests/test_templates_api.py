"""Tests for the template API on the FastAPI server.

Three routes:

* ``GET /api/templates``            -> the grouped catalog;
* ``GET /api/templates/{name}``     -> one template + workflow + input shape;
* ``POST /api/workspaces/from-template`` -> create a workspace AND start the
  task, returning both so the UI can go straight to the canvas.

The end-to-end proof lives here too: pick a template, create a workspace from
it, and confirm the run reaches ``completed`` with artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app

@pytest.fixture()
def app(tmp_path: Path):
    return create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)

@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client

# --------------------------------------------------------------------------- #
# GET /api/templates
# --------------------------------------------------------------------------- #

def test_list_templates_returns_the_grouped_catalog(client: TestClient):
    res = client.get("/api/templates")
    assert res.status_code == 200
    groups = res.json()
    assert isinstance(groups, list) and groups
    for group in groups:
        assert set(group) == {
            "moduleName", "category", "title", "icon", "type", "templates",
        }
        assert group["templates"], "no empty categories"

def test_list_templates_matches_the_catalog_module(client: TestClient):
    from uap.templates import catalog

    assert client.get("/api/templates").json() == catalog()

# --------------------------------------------------------------------------- #
# GET /api/templates/{name}
# --------------------------------------------------------------------------- #

def test_get_template_detail(client: TestClient):
    res = client.get("/api/templates/research_literature_review")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "research_literature_review"
    assert body["workflow"]["domain"] == "research"
    assert body["workflow"]["workflow_ref"] == "research"
    assert body["input"]["shape"]["input"].startswith("research ")

def test_get_template_unknown_is_404(client: TestClient):
    res = client.get("/api/templates/not_a_template")
    assert res.status_code == 404

def test_every_catalog_template_is_fetchable(client: TestClient):
    from uap.templates import TEMPLATE_NAMES

    for name in TEMPLATE_NAMES:
        res = client.get(f"/api/templates/{name}")
        assert res.status_code == 200, name
        assert res.json()["name"] == name

# --------------------------------------------------------------------------- #
# POST /api/workspaces/from-template
# --------------------------------------------------------------------------- #

def test_from_template_creates_workspace_and_starts_task(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={
            "template": "research_literature_review",
            "input": "research the literature on retrieval augmented generation",
        },
    )
    assert res.status_code == 200
    body = res.json()

    # The workspace is real and the task is real.
    assert body["template"] == "research_literature_review"
    assert body["workspace"]["id"]
    assert body["workspace"]["name"]
    assert body["task"]["task_id"]
    assert body["task"]["status"] == "accepted"
    assert body["task"]["domain"] == "research"
    assert body["task"]["workflow"] == "ResearchWorkflow"

    # The task actually runs to completion (run_inline=True).
    detail = client.get(f"/tasks/{body['task']['task_id']}").json()
    assert detail["status"] == "completed"

def test_from_template_unknown_template_is_404(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={"template": "nope", "input": "research something"},
    )
    assert res.status_code == 404

def test_from_template_requires_input(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={"template": "research_literature_review", "input": ""},
    )
    assert res.status_code == 422

def test_from_template_input_that_clarifies_is_reported_not_crashed(client: TestClient):
    """An input the entry workflow cannot route returns the question, honestly."""
    res = client.post(
        "/api/workspaces/from-template",
        json={"template": "research_literature_review", "input": "hello there"},
    )
    assert res.status_code == 200
    body = res.json()
    # Workspace still created (the user chose a template), but no task started.
    assert body["workspace"]["id"]
    assert body["task"] is None
    assert body["status"] == "clarification"
    assert body["question"]

def test_from_template_bbp_recon_sweep_runs_end_to_end(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={
            "template": "bbp_recon_sweep",
            "input": "bug bounty recon on example.com. In scope: *.example.com",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["task"]["domain"] == "bbp"
    assert body["task"]["workflow"] == "BBPWorkflow"

    detail = client.get(f"/tasks/{body['task']['task_id']}").json()
    assert detail["status"] == "completed"
    assert {"findings.json", "report.md"} <= {a["type"] for a in detail["artifacts"]}

def test_from_template_workspace_is_listed_after_creation(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={
            "template": "research_technology_comparison",
            "input": "research and compare PostgreSQL vs SQLite for durable agent state",
        },
    )
    assert res.status_code == 200
    workspace_id = res.json()["workspace"]["id"]

    listed = client.get("/api/workspaces").json()
    assert any(ws["id"] == workspace_id for ws in listed)

def test_from_template_workspace_pins_the_workflow_ref(client: TestClient):
    res = client.post(
        "/api/workspaces/from-template",
        json={
            "template": "bbp_assisted_assessment",
            "input": "bug bounty on example.com. In scope: *.example.com",
        },
    )
    assert res.status_code == 200
    workspace = res.json()["workspace"]
    assert workspace["default_workflow_refs"] == ["bbp"]

# --------------------------------------------------------------------------- #
# Auth: the new routes honour the same token gate as the rest of /api
# --------------------------------------------------------------------------- #

def test_from_template_is_401_without_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UAP_API_TOKEN", "secret-token")
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    with TestClient(app) as client:
        res = client.post(
            "/api/workspaces/from-template",
            json={"template": "research_literature_review", "input": "research x"},
        )
        assert res.status_code == 401
