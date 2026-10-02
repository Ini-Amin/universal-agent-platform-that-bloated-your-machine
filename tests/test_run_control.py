"""Tests for cooperative pause/resume (in-memory runs)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app
from uap.server.run_control import RunControl, get_control, run_controls


# -- Unit tests for RunControl ----------------------------------------------- #


def test_run_control_starts_running():
    ctrl = RunControl("t1")
    assert ctrl.status == "running"


def test_run_control_pause_resume():
    ctrl = RunControl("t2")
    assert ctrl.pause() == "paused"
    assert ctrl.status == "paused"
    assert ctrl.resume() == "running"
    assert ctrl.status == "running"


def test_run_control_completed_ignores_pause():
    ctrl = RunControl("t3")
    ctrl.status = "completed"
    assert ctrl.pause() == "completed"


@pytest.mark.anyio
async def test_wait_if_paused_blocks_until_resume():
    ctrl = RunControl("t4")
    ctrl.pause()
    resumed = False

    async def waiter():
        nonlocal resumed
        await ctrl.wait_if_paused()
        resumed = True

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not resumed
    ctrl.resume()
    await asyncio.sleep(0.05)
    assert resumed
    await task


@pytest.mark.anyio
async def test_wait_if_paused_passes_when_running():
    ctrl = RunControl("t5")
    await ctrl.wait_if_paused()  # should not block


def test_get_control_creates_and_reuses():
    # Clean up any previous state.
    controls = run_controls()
    controls.pop("test-gc", None)
    c1 = get_control("test-gc")
    c2 = get_control("test-gc")
    assert c1 is c2
    controls.pop("test-gc", None)


# -- HTTP endpoint tests ----------------------------------------------------- #


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    with TestClient(app) as c:
        yield c


def test_pause_unknown_task(client: TestClient):
    res = client.post("/api/executions/nonexistent/pause")
    assert res.status_code == 200
    assert res.json()["status"] == "unknown"


def test_resume_unknown_task(client: TestClient):
    res = client.post("/api/executions/nonexistent/resume")
    assert res.status_code == 200
    assert res.json()["status"] == "unknown"


def test_pause_resume_completed_task(client: TestClient):
    # Create a task (runs inline, so it completes).
    task_res = client.post(
        "/tasks", json={"input": "research best practices"}
    ).json()
    task_id = task_res["task_id"]

    # Task completed; pause should report not_running.
    res = client.post(f"/api/executions/{task_id}/pause")
    assert res.status_code == 200
    assert res.json()["status"] == "not_running"
