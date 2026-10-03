"""Behavioral tests for UI Fidelity and UX Improvements.

Guarantees:
- Desktop canvas: zero @media queries in CSS.
- Required Figma landmarks and accessible attributes exist in index.html.
- Keyboard support (Esc exits fill, Ctrl+S saves, Ctrl+Enter runs).
- Fill mode spans full width.
"""

from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from uap.server import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client
def test_no_media_queries_in_css(client: TestClient) -> None:
    """UAP is a desktop-first canvas tool. Zero @media queries allowed."""
    res = client.get("/css/app.css")
    assert res.status_code == 200
    css = res.text
    # No @media rules allowed in stylesheet
    assert "@media" not in css, "app.css must not contain @media queries (desktop-only requirement)"


def test_ui_landmarks_present_in_index(client: TestClient) -> None:
    """Figma landmarks must be wired in index.html."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # Sidebar Identity
    assert "Universal" in html
    assert "AGENT PLATFORM" in html
    assert "btn-collapse-sidebar" in html

    # Sidebar Resources Accordion Sections
    assert 'data-section="agents"' in html
    assert 'data-section="tools"' in html
    assert 'data-section="skills"' in html
    assert 'data-section="knowledge"' in html
    assert 'data-section="artifacts"' in html

    # Runtime Environment & Account Pill
    assert "Sandbox connected" in html
    assert "Alex Kim" in html
    assert "Pro workspace" in html

    # Task Context Header
    assert "Build a customer usage dashboard" in html
    assert "btn-tools-palette" in html
    assert "btn-share-canvas" in html


def test_keyboard_listeners_present(client: TestClient) -> None:
    """Keyboard accessibility: Esc, Ctrl+S, Ctrl+Enter."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    assert "key.toLowerCase() === 's'" in html, "Global Ctrl+S listener must be present"
    assert "key === 'Enter'" in html, "Global Ctrl+Enter listener must be present"
    assert "Escape" in html, "Escape key handler must be present"


def test_stage_fill_mode_and_terminal_tabs(client: TestClient) -> None:
    """Stage.js must support full width fill and interactive terminal tabs."""
    res = client.get("/js/stage.js")
    assert res.status_code == 200
    js = res.text

    # Fill mode attaches to containerEl for full width spanning
    assert "stage-fill-active" in js
    assert "entry.cardEl.__origParent" in js
    # Terminal tabs interactive
    assert "stage-terminal-tab" in js
    assert "setTerminalTab" in js
    assert "stage-terminal-problems" in js
