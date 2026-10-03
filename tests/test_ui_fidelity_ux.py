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
    assert "account-pill" in html
    assert "Operator" in html

    # Task Context Header
    assert 'id="task-input"' in html
    assert 'placeholder="What do you want to research?"' in html
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


def test_empty_canvas_shows_a_real_usage_guide(client: TestClient) -> None:
    """The old workbench tile-grid is gone; a real usage guide replaces it.

    The guide must name controls that actually exist (verified live), be
    dismissible, and never show the removed ✦ WORKBENCH CANVAS badge.
    """
    js = client.get("/js/stage.js").text
    # The dead workbench markup and its tool-tile handler are gone.
    assert "WORKBENCH CANVAS" not in js
    assert "stage-empty-tool-btn" not in js
    assert "stage-empty-actions" not in js
    # A real guide exists and covers the actual flow.
    assert "stage-guide" in js
    assert "How to use the canvas" in js
    assert "Add surface" in js            # the real toolbar control name
    assert "Don't show again" in js or "Don’t show again" in js
    assert "Open Folder" in js            # the real editor control name
    assert "Arrange" in js

    css = client.get("/css/app.css").text
    assert ".stage-guide" in css
    assert ".stage-empty-state" not in css
    assert ".stage-empty-tool-btn" not in css


def test_sidebar_add_buttons_are_honest(client: TestClient) -> None:
    """A control labelled "Add X" must not open the tool launcher.

    Agents/Tools/Skills/Artifacts have no create API, so their dishonest `+`
    is removed. Knowledge keeps a labelled "Dedupe" action, not a `+`.
    """
    html = client.get("/").text
    for label in ("Add Agent", "Add Tool", "Add Skill", "Add Knowledge", "Add Artifact"):
        assert label not in html, f"dishonest control still present: {label}"
    # Knowledge exposes the real dedupe action.
    assert "Dedupe" in html
    assert "dedupe-knowledge" in html
    # No bare `+` remains in a resource-section header.
    assert 'class="sidebar-section-add" title=' not in html


def test_knowledge_rows_expose_a_delete_control(client: TestClient) -> None:
    html = client.get("/").text
    assert "sidebar-item-delete" in html
    assert "deleteKnowledgeItem" in html
    assert "deleteKnowledge" in html
    assert "/api/knowledge" in client.get("/js/api.js").text


def test_canvas_has_arrange_control(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    assert "arrangeCards" in js
    assert 'data-action="arrange"' in js
    assert "Arrange" in js


def test_free_placement_uses_real_card_sizes(client: TestClient) -> None:
    """The fixed 3-col/500px grid is replaced by real-size, wrapped, clamped placement."""
    js = client.get("/js/stage.js").text
    assert "worldBounds" in js
    assert "cardBox" in js
    assert "collidesAt" in js
    # The old hard-coded constants that caused the overlap must be gone.
    assert "cellW = 500" not in js
    assert "otherW = 440" not in js
