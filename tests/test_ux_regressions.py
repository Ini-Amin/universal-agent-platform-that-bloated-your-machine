"""Regression tests for known UAP UX bugs.

Each test targets a specific, previously-observed defect. They are written to
FAIL on the buggy code and PASS once the fix lands. Tests 2 and 3 are static
file assertions; test 1 exercises the real /tasks endpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Test 1: GET /tasks must not return duplicate task_ids
# --------------------------------------------------------------------------- #

def test_tasks_endpoint_returns_unique_ids(tmp_path: Path):
    """The listing endpoint must return each task exactly once.

    Regression: the in-memory ``runs`` registry stores one RunRecord under two
    keys (task_id AND execution_id); iterating ``values()`` without dedup made
    ``GET /tasks`` emit duplicate task_ids.
    """
    try:
        app = create_app(
            runs_dir=tmp_path / "runs",
            run_inline=True,
            heartbeat_interval=0.05,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"create_app unavailable (PostgreSQL/backend missing): {exc!r}")

    with TestClient(app) as client:
        # MUST be an input that actually routes. "summarize the release notes"
        # returns a clarification (no task registered), so the listing was empty
        # and the uniqueness assertion passed vacuously — it proved nothing.
        # Only `research ...` and `bug bounty on ...` are executable today.
        response = client.post(
            "/tasks",
            json={
                "input": "research checkpoint strategies for agent workflows",
                "user_id": "tester",
            },
        )
        if response.status_code >= 500:
            pytest.skip(f"POST /tasks failed (backend unavailable): {response.status_code}")

        assert response.status_code == 200, response.text

        listing = client.get("/tasks")
        assert listing.status_code == 200, listing.text
        payload = listing.json()

    tasks = payload["tasks"] if isinstance(payload, dict) else payload
    ids = [t["task_id"] for t in tasks]

    # Guard against a vacuous pass: if nothing was registered, the uniqueness
    # assertion below would hold trivially and hide a regression.
    assert ids, f"no task was registered for a valid research input; listing={payload!r}"
    assert len(ids) == len(set(ids)), f"duplicate task_ids in GET /tasks: {ids}"


# --------------------------------------------------------------------------- #
# Test 2: UI must carry an AGPL-3.0 source link (license section 13)
# --------------------------------------------------------------------------- #

def test_ui_has_agpl_source_link():
    """The AGPL requires offering the Corresponding Source to network users."""
    html = (PROJECT_ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "href=" in html, "ui/index.html has no links at all"
    assert "github.com" in html, (
        "ui/index.html must link to the project repository (github.com) for "
        "AGPL-3.0 section 13 source-offer compliance"
    )


# --------------------------------------------------------------------------- #
# Test 3: CSS must allow long paths to wrap
# --------------------------------------------------------------------------- #

def test_css_wraps_long_paths():
    """PROXY check only -- this asserts a wrapping rule is PRESENT, not that
    any element actually renders wrapped. A real rendering check would need a
    browser; this guards against regressing to one-character-per-line layout.
    """
    css = (PROJECT_ROOT / "ui" / "css" / "app.css").read_text(encoding="utf-8")

    assert any(
        token in css for token in ("overflow-wrap", "word-break", "min-width: 0")
    ), "ui/css/app.css must contain a long-path wrapping rule (overflow-wrap/word-break/min-width: 0)"