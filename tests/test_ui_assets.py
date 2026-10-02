"""Static asset and contract tests for the UAP Canvas UI (§52–§55).

Verifies that the UI serves the toast notification system, clarification flow,
error-box styling, and the Output & Artifacts inspector panel without alerts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        heartbeat_interval=0.05,
    )


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


# 1. Served index.html contains no alert() calls
def test_index_html_has_no_alert_calls(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "alert(" not in res.text


# 2. Served index.html imports toast.js and initializes toast container
def test_index_html_references_toast_system(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "toast.js" in res.text
    assert 'id="toast-container"' in res.text
    assert "initToasts" in res.text


# 3. toast.js is served and exports showToast and initToasts
def test_toast_js_served_and_exports_apis(client: TestClient) -> None:
    res = client.get("/js/toast.js")
    assert res.status_code == 200
    assert "export function showToast" in res.text
    assert "export function initToasts" in res.text
    assert "uiStore" in res.text


# 4. app.css contains toast styles and animation
def test_app_css_contains_toast_classes(client: TestClient) -> None:
    res = client.get("/css/app.css")
    assert res.status_code == 200
    assert ".toast-container" in res.text
    assert ".toast-clarify" in res.text
    assert ".toast-error" in res.text
    assert "toast-slide-in" in res.text


# 5. app.css contains input validation and error-box styles
def test_app_css_contains_error_and_validation_styles(client: TestClient) -> None:
    res = client.get("/css/app.css")
    assert res.status_code == 200
    assert ".input-error" in res.text
    assert ".error-box" in res.text
    assert ".output-code-block" in res.text


# 6. inspector.js contains Output & Artifacts section renderer and renders execState.output
def test_inspector_js_renders_output_and_artifacts(client: TestClient) -> None:
    res = client.get("/js/inspector.js")
    assert res.status_code == 200
    assert "Output &amp; Artifacts" in res.text or "Output & Artifacts" in res.text
    assert "execState.output" in res.text
    assert "btn-copy-output" in res.text


# 7. inspector.js renders artifacts list and supports copy
def test_inspector_js_handles_artifacts_and_copy(client: TestClient) -> None:
    res = client.get("/js/inspector.js")
    assert res.status_code == 200
    assert "execState.artifacts" in res.text
    assert "artifacts-list" in res.text
    assert "btn-copy-artifact" in res.text


# 8. state.js includes artifacts in executionStore initial state
def test_state_js_includes_artifacts_in_store(client: TestClient) -> None:
    res = client.get("/js/state.js")
    assert res.status_code == 200
    assert "artifacts: []" in res.text


# 9. index.html guards Ctrl+Z/Ctrl+Y in text inputs and contenteditable
def test_index_html_guards_keyboard_shortcuts_in_inputs(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "isContentEditable" in res.text
    assert "INPUT" in res.text
    assert "TEXTAREA" in res.text


# 10. app.css defines all required status badges, error-box, and focus-visible ring
def test_app_css_contains_badges_and_focus_visible(client: TestClient) -> None:
    res = client.get("/css/app.css")
    assert res.status_code == 200
    assert ".badge-idle" in res.text
    assert ".badge-connecting" in res.text
    assert ".badge-pending" in res.text
    assert ".badge-skipped" in res.text
    assert ".error-box" in res.text
    assert ":focus-visible" in res.text


# 11. Canvas includes Fit-to-view button and centered zoom controller
def test_canvas_has_fit_to_view(client: TestClient) -> None:
    html = client.get("/").text
    assert "btn-zoom-fit" in html
    assert "fitToView" in html

    js = client.get("/js/graph-canvas.js").text
    assert "fitToView" in js
    assert "zoomAt" in js


# 12. Event console renders empty state and WebSocket connection state is surfaced
def test_console_empty_state_and_ws_status(client: TestClient) -> None:
    stream_js = client.get("/js/event-stream.js").text
    assert "console-empty" in stream_js
    assert "No events yet" in stream_js
    assert "connectionStatus" in stream_js

    html = client.get("/").text
    assert "ws-status-badge" in html

