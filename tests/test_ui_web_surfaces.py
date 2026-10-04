"""Web Page and Search surfaces: the add-surface form, the framing notice, the search card.

Reproduced in a browser before fixing: typing ``google.com`` was rejected by the
native ``type=url`` bubble ("Please enter a URL."); ``https://google.com`` showed the
browser's "refused to connect" page because Google sends ``X-Frame-Options``; the
launcher popover ran past the pane on a 1440px screen and focusing its field scrolled
the whole canvas sideways; and Search was a static "not configured" card.

The pure logic is run under Node (skipped when Node is missing); the rest are static
contracts on the served assets, like the other ``test_ui_*`` modules.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)
    with TestClient(app) as test_client:
        yield test_client


def _node(module: str, expression: str):
    """Evaluate a JS expression (``m`` is the imported module) under Node; return its JSON."""
    script = f"import('{(UI_JS / module).as_uri()}').then((m) => console.log(JSON.stringify({expression})))"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.google.com/search?q=x", "google.com"),
        ("https://duckduckgo.com/", "duckduckgo.com"),
        ("https://github.com/", "github.com"),
        ("https://gist.github.com/x", "github.com"),
        ("https://www.youtube.com/watch?v=1", "youtube.com"),
        # made for embedding
        ("https://www.youtube.com/embed/abc", None),
        ("https://www.google.com/maps/embed?pb=1", None),
        # no framing restriction
        ("https://example.com", None),
        ("https://en.wikipedia.org/wiki/Python", None),
        # a suffix match must respect the dot boundary
        ("https://notgoogle.com", None),
        ("https://foo.github.io", None),
        # not an http(s) address
        ("/relative", None),
        ("data:text/html,hi", None),
        ("javascript:alert(1)", None),
    ],
)
def test_framing_heuristic(url: str, expected: str | None) -> None:
    assert _node("stage.js", f"m.framingBlockedHost({json.dumps(url)})") == expected


@needs_node
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("google.com", "https://google.com"),
        ("  example.com/a b ", ""),
        ("example.com/page?x=1", "https://example.com/page?x=1"),
        ("https://example.com/x", "https://example.com/x"),
        ("http://example.com", "http://example.com"),
        ("localhost:3000", "http://localhost:3000"),
        ("192.168.1.5:8080/x", "http://192.168.1.5:8080/x"),
        ("/relative/page", "/relative/page"),
        ("data:text/html,<b>hi</b>", "data:text/html,<b>hi</b>"),
        ("hello world", ""),
        ("hello", ""),
        ("ftp://example.com", ""),
        ("javascript:alert(1)", ""),
        ("", ""),
    ],
)
def test_normalize_tool_url(raw: str, expected: str) -> None:
    assert _node("url.js", f"m.normalizeToolUrl({json.dumps(raw)})") == expected


@needs_node
def test_search_url_is_encoded_and_falls_back_to_the_default_engine() -> None:
    assert _node("search.js", "m.searchUrl('google', 'a b&c')") == "https://www.google.com/search?q=a%20b%26c"
    assert _node("search.js", "m.searchUrl('nope', 'x')") == "https://duckduckgo.com/?q=x"
    assert _node("search.js", "m.searchUrl('bing', '  padded  ')") == "https://www.bing.com/search?q=padded"


@needs_node
def test_filter_knowledge_matches_every_word_across_statement_domain_and_tags() -> None:
    items = [
        {"statement": "Checkpointing bounds replay cost", "domain": "agents", "tags": ["durability"]},
        {"statement": "pgvector supports cosine indexes", "domain": "databases", "tags": []},
    ]
    expr = f"m.filterKnowledge({json.dumps(items)}, %s).map((i) => i.domain)"
    assert _node("search.js", expr % json.dumps("replay agents")) == ["agents"]
    assert _node("search.js", expr % json.dumps("DURABILITY")) == ["agents"]
    assert _node("search.js", expr % json.dumps("cosine replay")) == []
    assert _node("search.js", expr % json.dumps("")) == ["agents", "databases"]
    assert _node("search.js", "m.filterKnowledge(null, 'x')") == []


# ---------------------------------------------------------------------------
# Served assets
# ---------------------------------------------------------------------------

def test_the_add_surface_form_no_longer_blocks_addresses_without_a_scheme(client: TestClient) -> None:
    html = client.get("/").text
    # type=url + required made the browser reject "google.com" before our own
    # normalisation (which adds https://) could run.
    assert 'type="url" id="tool-input-url"' not in html
    assert 'id="tool-input-url"' in html
    assert "normalizeToolUrl(url)" in html
    assert "That does not look like a web address" in html


def test_the_launcher_popover_is_clamped_inside_the_pane(client: TestClient) -> None:
    html = client.get("/").text
    # It was clamped against the window width in the wrong coordinate space, so the
    # form overflowed the pane and focusing its field scrolled the canvas sideways.
    assert "canvasEl.clientWidth" in html
    assert "focus({ preventScroll: true })" in html


def test_web_page_card_explains_sites_that_refuse_embedding(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    assert "export function framingBlockedHost" in js
    assert "stage-embed-blocked" in js
    assert "Try embedding anyway" in js
    # The Back button had no handler, and the padlock showed for plain http.
    assert "stage-browser-back" not in js
    assert "Not secure (http)" in js
    css = client.get("/css/app.css").text
    assert ".stage-embed-blocked" in css


def test_search_is_a_working_surface_not_a_not_configured_card(client: TestClient) -> None:
    html = client.get("/").text
    stage_js = client.get("/js/stage.js").text
    assert "SEARCH_STATUS_MARKDOWN" not in html
    assert "SEARCH_STATUS_MARKDOWN" not in stage_js
    assert "Search Provider Not Configured" not in stage_js
    assert "registerViewBuilder('search'" in html
    assert "kind: 'search'" in html
    # An already-saved static card is upgraded instead of lingering after a reload.
    assert "s.id === 'tool-search' && s.kind === 'markdown'" in html

    search_js = client.get("/js/search.js")
    assert search_js.status_code == 200
    assert "export function buildSearchBody" in search_js.text
    # Result text must never be inserted as markup.
    assert "innerHTML" not in search_js.text
    assert ".stage-search" in client.get("/css/app.css").text
