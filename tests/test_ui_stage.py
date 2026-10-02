"""Tests for the canvas-as-stage view host (§52/§55).

The centre pane used to render exactly one thing — an SVG node graph — so the
product could say a workflow ran but never show what it produced. ``ui/js/stage.js``
is the fix: a host that renders arbitrary tool views (html, markdown, image,
video, iframe, code, whiteboard, placeholder).

These tests do two things:

1. Static contracts on the served assets (index.html wiring, app.css layout) —
   these always run.
2. Behavioral contracts by running the REAL ``ui/js/stage.js`` under Bun with a
   linkedom DOM (skips cleanly when Bun or linkedom is unavailable, matching the
   repo's existing UI-JS test convention).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uap.server import create_app

UI_DIR = Path(__file__).resolve().parents[1] / "ui"
UI_JS = UI_DIR / "js"
UI_CSS = UI_DIR / "css"

BUN = shutil.which("bun")


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------

def test_stage_js_is_served_and_exports_the_api(client: TestClient) -> None:
    res = client.get("/js/stage.js")
    assert res.status_code == 200
    assert "export function createStage" in res.text
    assert "export function renderMarkdown" in res.text
    # every kind has an explicit branch
    for kind in ("html", "markdown", "image", "video", "iframe", "code", "whiteboard", "placeholder"):
        assert f"case '{kind}'" in res.text, f"stage.js has no branch for kind={kind}"
    # the whiteboard URL was verified in a browser, not guessed
    assert "https://excalidraw.com/" in res.text


def test_index_html_hosts_the_stage_and_narrow_strip(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="stage"' in html
    assert 'id="node-strip"' in html
    assert 'id="node-strip-list"' in html
    assert 'id="strip-reveal"' in html
    assert "from './js/stage.js'" in html
    assert "createStage(" in html
    # the graph renderer container stays in the DOM (its logic keeps working)
    assert 'id="graph-canvas-container"' in html
    # node chips open a view
    assert "node-chip" in html
    assert "kind: 'placeholder'" in html


def test_app_css_defines_strip_and_collapse_layout(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    assert ".node-strip" in css
    assert ".node-chip" in css
    assert ".canvas-stage-area" in css
    # ~72-96px narrow strip
    assert "width: 84px" in css
    # collapse behaviour: the strip goes to zero and the left-edge handle appears
    assert ".canvas-stage-area.strip-collapsed .node-strip" in css
    assert ".strip-reveal" in css
    assert "body.stage-fullscreen .canvas-stage-area.strip-collapsed .strip-reveal" in css


def test_index_html_wires_fullscreen_and_strip_toggle(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="btn-fullscreen-stage"' in html
    assert 'id="btn-toggle-strip"' in html
    assert "fullscreenchange" in html
    assert "requestFullscreen" in html
    assert "setStripCollapsed" in html


# ---------------------------------------------------------------------------
# Behavioral contracts (real stage.js under a real DOM)
# ---------------------------------------------------------------------------

def _find_linkedom_root() -> Path | None:
    """Locate a node_modules directory containing the linkedom package."""
    candidates: list[Path] = []
    npm = shutil.which("npm")
    if npm:
        try:
            r = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                g = Path(r.stdout.strip())
                candidates.append(g)
                candidates.extend(p for p in g.glob("*/node_modules") if p.is_dir())
        except Exception:
            pass
    for base in (Path.home() / ".npm-global" / "lib" / "node_modules", Path("/usr/lib/node_modules")):
        if base.is_dir():
            candidates.append(base)
            candidates.extend(p for p in base.glob("*/node_modules") if p.is_dir())
    for c in candidates:
        if (c / "linkedom" / "package.json").exists():
            return c
    return None


_LINKEDOM_ROOT = _find_linkedom_root()

pytestmark_dom = pytest.mark.skipif(
    BUN is None or _LINKEDOM_ROOT is None,
    reason="needs Bun + linkedom to run ui/js/stage.js against a DOM",
)

_STAGE_SCRIPT = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML("<!doctype html><html><body><div id='host'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;

const { createStage, renderMarkdown } = await import("./stage.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// --- markdown renderer (no dependency) ---------------------------------
const md = renderMarkdown(
  "# Title\n\n**bold** and `code` and [link](https://example.com)\n\n- one\n- two\n\n> quote\n\n```python\nx = 1\n```"
);
check("md heading", md.includes("<h1>Title</h1>"));
check("md bold", md.includes("<strong>bold</strong>"));
check("md inline code", md.includes("<code>code</code>"));
check("md link", md.includes('href="https://example.com"'));
check("md list", md.includes("<li>one</li>") && md.includes("<li>two</li>"));
check("md quote", md.includes("<blockquote>"));
check("md fence", md.includes('class="md-code"') && md.includes("x = 1"));
check("md escapes html", renderMarkdown("<script>x</script>").includes("&lt;script&gt;"));

// --- stage lifecycle ----------------------------------------------------
const host = document.getElementById("host");
const stage = createStage(host);
const events = [];
stage.onEvent((e) => events.push(e.type));

const ids = [
  stage.showView({ kind: "html", id: "h", title: "H", html: "<b>hi</b>" }),
  stage.showView({ kind: "markdown", id: "m", title: "M", markdown: "**b**" }),
  stage.showView({ kind: "image", id: "i", title: "I", url: "https://example.com/a.png", alt: "a" }),
  stage.showView({ kind: "video", id: "v", title: "V", url: "https://example.com/a.mp4" }),
  stage.showView({ kind: "iframe", id: "f", title: "F", url: "https://example.com/" }),
  stage.showView({ kind: "code", id: "c", title: "C", language: "bash", text: "echo hi" }),
  stage.showView({ kind: "whiteboard", id: "w", title: "W" }),
  stage.showView({ kind: "placeholder", id: "p", title: "P", message: "later" }),
  stage.showView({ kind: "mystery-kind", id: "u", title: "U" }),
];
check("ids returned", ids.every((x) => typeof x === "string" && x.length > 0));
check("one card per view", document.querySelectorAll(".stage-card").length === 9);
check("every card has a titlebar", document.querySelectorAll(".stage-card-titlebar").length === 9);
check("every card has a close button", document.querySelectorAll(".stage-card-close").length === 9);

const card = (id) => document.querySelector(`.stage-card[data-view-id="${id}"]`);
check("html renders markup", card("h").querySelector("b")?.textContent === "hi");
check("markdown renders", card("m").querySelector("strong")?.textContent === "b");
check("image src", card("i").querySelector("img")?.getAttribute("src") === "https://example.com/a.png");
check("image alt", card("i").querySelector("img")?.getAttribute("alt") === "a");
const vid = card("v").querySelector("video");
check("video tag", vid?.tagName === "VIDEO");
check("video controls", vid?.hasAttribute("controls") === true);
check("iframe src", card("f").querySelector("iframe")?.getAttribute("src") === "https://example.com/");
check("iframe sandbox default", (card("f").querySelector("iframe")?.getAttribute("sandbox") || "").includes("allow-scripts"));
check("code text", card("c").querySelector("pre code")?.textContent === "echo hi");
check("code copy button", card("c").querySelector(".stage-code-copy")?.textContent === "Copy");
check("whiteboard is an excalidraw iframe", card("w").querySelector("iframe")?.getAttribute("src") === "https://excalidraw.com/");
check("placeholder message", card("p").querySelector(".stage-placeholder-message")?.textContent === "later");
check("unknown kind does not fail silently", /Unsupported view kind/.test(card("u").querySelector(".stage-placeholder-message")?.textContent || ""));

// --- listViews / closeView / clear / update -----------------------------
const list = stage.listViews();
check("listViews shape", list.length === 9 && list.every((v) => v.id && v.kind && v.title));
check("closeView returns true", stage.closeView("h") === true);
check("closeView removed card", card("h") === null && stage.listViews().length === 8);
check("closeView unknown returns false", stage.closeView("nope") === false);
check("clear empties", stage.clear() === 8 && stage.listViews().length === 0);
check("events emitted", events.includes("open") && events.includes("close") && events.includes("clear"));

// showView with the same id replaces rather than duplicating
stage.showView({ kind: "code", id: "dup", title: "A", text: "1" });
stage.showView({ kind: "code", id: "dup", title: "B", text: "2" });
check("same id replaces", document.querySelectorAll('.stage-card[data-view-id="dup"]').length === 1);
check("same id updated", card("dup").querySelector("pre code")?.textContent === "2");

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_STAGE_CONTRACTS_OK");
"""


@pytestmark_dom
def test_stage_contracts_under_real_dom(tmp_path: Path) -> None:
    """Run the real ui/js/stage.js against a linkedom DOM and assert every kind."""
    shutil.copy(UI_JS / "stage.js", tmp_path / "stage.js")
    (tmp_path / "check_stage.mjs").write_text(_STAGE_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)

    result = subprocess.run(
        [BUN, str(tmp_path / "check_stage.mjs")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, (
        f"stage contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ALL_STAGE_CONTRACTS_OK" in result.stdout
