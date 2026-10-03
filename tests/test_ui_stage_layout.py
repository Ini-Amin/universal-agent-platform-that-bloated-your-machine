"""Stage layout and card fixes found by the browser audit (ui/js/stage.js, ui/css/app.css).

Covers: new cards spreading over the Split 2 / Split 4 regions instead of piling
up in one (S1), Split 4 cards fitting their region (S2), the stage no longer
scrolling its host (S3), narrow title bars keeping title and close button (S4),
readable media failures (S5), Undo for a closed Note (S6) and a readable
placeholder colour (S7).

Static contracts on the served assets always run. The placement, Undo and media
contracts also run the REAL ``ui/js/stage.js`` under Bun with a linkedom DOM
(skipped cleanly when Bun or linkedom is unavailable, like the other stage tests).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"

BUN = shutil.which("bun")
NODE = shutil.which("node")


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _css(client: TestClient) -> str:
    res = client.get("/css/app.css")
    assert res.status_code == 200
    return re.sub(r"/\*.*?\*/", "", res.text, flags=re.S)


def _rules(css: str, selector: str) -> str:
    """Declarations of every top-level rule whose selector is exactly ``selector``."""
    pattern = r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}"
    return "\n".join(re.findall(pattern, css))


def _container_rules(css: str) -> dict[str, int]:
    """``@container (max-width: N)`` rules that set ``display: none``: selector -> N."""
    found: dict[str, int] = {}
    for width, selector, body in re.findall(
        r"@container\s*\(max-width:\s*(\d+)px\)\s*\{\s*([^{}]+?)\s*\{([^{}]*)\}\s*\}", css
    ):
        if "display: none" in body:
            found[selector.strip()] = int(width)
    return found


def _display_rule_offsets(css: str, selector: str) -> list[int]:
    """Offsets of the top-level rules for ``selector`` that set ``display``."""
    pattern = r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}"
    return [m.start() for m in re.finditer(pattern, css) if "display" in m.group(1)]


def _function_body(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    open_at = js.index("{", js.index(")", start))
    depth = 0
    for i in range(open_at, len(js)):
        depth += {"{": 1, "}": -1}.get(js[i], 0)
        if depth == 0:
            return js[open_at : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------

def test_split_placement_picks_a_region_by_load(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    assert "leastLoadedRegion(" in _function_body(js, "loadSplitRegion")
    # the old rule sent everything except a few kinds to region 0
    assert "kind ===" not in _function_body(js, "loadSplitRegion")
    # and every new card started at {0, 0}: the cascade must apply to all kinds
    assert "splitCascadePos(" in _function_body(js, "defaultSplitPos")
    assert "y: 0" not in _function_body(js, "defaultSplitPos")


def test_split_cards_are_sized_to_the_room_left_in_their_region(client: TestClient) -> None:
    css = _css(client)
    split4 = _rules(css, ".stage-grid-split-4 .stage-card")
    # `width: 100%` plus a cascade offset pushed the close button and grip out of the region
    assert not re.search(r"(?<![\w-])(?:width|height):\s*100%", split4)
    assert "var(--card-x" in split4 and "var(--card-y" in split4
    # split 2 caps a cascaded card at the bottom of its region too
    assert "var(--card-y" in _rules(css, ".stage-grid-split-2 .stage-card")


def test_split_2_kinds_do_not_pin_a_top_that_covers_cascaded_cards(client: TestClient) -> None:
    css = _css(client)
    per_kind = re.findall(
        r'(\.stage-grid-split-2 \.stage-card\[data-kind="(?:editor|code|terminal|iframe|whiteboard)"\][^{]*)\{([^{}]*)\}',
        css,
    )
    assert per_kind, "the per-kind split 2 rules disappeared"
    for selector, body in per_kind:
        assert "top:" not in body, f"{selector} pins its own top"


def test_stage_clip_is_not_overridden_by_the_workspace_columns(client: TestClient) -> None:
    css = _css(client)
    # `overflow: visible` here beat `.stage { overflow: clip }` and let cards scroll
    # `.canvas-stage-area`, pushing the zoom controls out of reach
    assert "overflow" not in _rules(css, ".workspace-columns .stage")
    assert "overflow: clip" in _rules(css, ".stage")


def test_narrow_card_title_bars_keep_the_title_and_close_button(client: TestClient) -> None:
    css = _css(client)
    assert "container-type: inline-size" in _rules(css, ".stage-card-titlebar")
    hidden = _container_rules(css)
    assert {".stage-card-kind", ".stage-card-actions"} <= set(hidden), hidden
    assert hidden[".stage-card-kind"] > hidden[".stage-card-actions"]  # the badge goes first
    # a container rule only wins if it comes after the rule that sets `display`
    for selector, width in hidden.items():
        container_at = css.index(f"@container (max-width: {width}px)")
        assert all(at < container_at for at in _display_rule_offsets(css, selector)), selector
    assert ".stage-card-close" not in hidden and ".stage-card-title" not in hidden


def test_media_failures_are_announced_and_wrap(client: TestClient) -> None:
    css = _css(client)
    js = client.get("/js/stage.js").text
    assert "overflow-wrap: anywhere" in _rules(css, ".stage-media-error")
    assert js.count('class="stage-media-error" role="alert"') == 2
    assert "Image failed to load: " in js and "Video failed to load: " in js


def test_placeholder_text_is_not_the_browser_default_grey(client: TestClient) -> None:
    css = _css(client)
    assert "var(--fg-muted)" in _rules(css, "::placeholder")


def test_a_closed_note_can_be_brought_back(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    html = client.get("/").text
    assert "close_undoable" in js and "close_undoable" in html
    assert "label: 'Undo'" in html
    assert "persistUserView({ ...evt.spec" in html
    # the note says where its text lives
    assert "statusEl.textContent = 'Saved in this browser'" in js


@pytest.mark.skipif(NODE is None, reason="needs node to syntax-check the inline module script")
def test_index_html_inline_module_script_parses(client: TestClient, tmp_path: Path) -> None:
    html = client.get("/").text
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, flags=re.S)
    assert scripts, "index.html lost its inline module script"
    script = tmp_path / "inline.mjs"
    script.write_text(scripts[-1], encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Behavioral contracts (real stage.js under a real DOM)
# ---------------------------------------------------------------------------

def _find_linkedom_root() -> Path | None:
    """Locate a node_modules directory containing the linkedom package."""
    candidates: list[Path] = [Path(p) for p in os.environ.get("NODE_PATH", "").split(os.pathsep) if p]
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

_PRELUDE = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML(
  "<!doctype html><html><body><div id='host'></div><div id='host2'></div><div id='host3'></div></body></html>"
);
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;

// In-memory localStorage so remembered regions and positions are really exercised
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { createStage } = await import("./stage.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const open = (stage, id, kind = "html") => stage.showView({ kind, id, title: id, html: "<b>x</b>" });
const regions = (stage, ids) => ids.map((id) => stage.getViewRegion(id)).join("");
const card = (id) => document.querySelector(`.stage-card[data-view-id="${id}"]`);
const tops = (root, ids) => ids.map((id) => root.querySelector(`.stage-card[data-view-id="${id}"]`).style.top).join();
const done = (marker) => {
  if (failures.length) {
    console.error("FAILURES: " + failures.join(", "));
    process.exit(1);
  }
  console.log(marker);
};
"""

_PLACEMENT_SCRIPT = _PRELUDE + r"""
const host = document.getElementById("host");
const host2 = document.getElementById("host2");
const host3 = document.getElementById("host3");

// --- Split 2 (the default): new cards alternate regions and cascade ----------
const s2 = createStage(host, { defaultSplitMode: 2 });
const ids2 = ["a", "b", "c", "d", "e"];
ids2.forEach((id) => open(s2, id));
check("split 2 spreads cards over both regions (ties: lowest index)", regions(s2, ids2) === "01010");
check("cards in a region cascade down", tops(host, ids2) === "16px,16px,52px,52px,88px");
check("the first card sits at the 16px origin", card("a").style.left === "16px");
check("the stage tells CSS how much room is taken", card("c").style.getPropertyValue("--card-x") === "52px"
  && card("c").style.getPropertyValue("--card-y") === "52px");
check("no 'Empty' hint once both regions hold a card",
  Array.from(host.querySelectorAll(".stage-region-empty")).every((el) => el.style.display === "none"));
// closing a card frees capacity: the next card goes to the least-loaded region
s2.closeView("b");
open(s2, "f");
check("least-loaded region wins after a close", s2.getViewRegion("f") === 1);

// --- Split 4: four regions fill before any region gets a second card --------
store.clear();
store.set("uap.stage.split.mode", "4");
const s4 = createStage(host2);
const ids4 = ["p", "q", "r", "s", "t", "u"];
ids4.forEach((id) => open(s4, id));
check("split 4 uses all four regions first", regions(s4, ids4) === "012301");
check("second cards cascade inside their region", tops(host2, ids4) === "16px,16px,16px,16px,52px,52px");

// --- a reload restores each card to its saved region and position ------------
s4.setViewRegion("q", 3);
const savedRegions = regions(s4, ids4);
const savedTops = tops(host2, ids4);
s4.disconnectCanvas();
const reloaded = createStage(host3);
ids4.forEach((id) => open(reloaded, id));
check("reload keeps the saved regions", regions(reloaded, ids4) === savedRegions);
check("reload keeps the saved positions", tops(host3, ids4) === savedTops);
reloaded.disconnectCanvas();

// --- Free -> Split 2 -> Split 4 -> Split 2 ----------------------------------
store.clear();
host3.innerHTML = "";
const free = createStage(host3);
const idsF = ["a", "b", "c", "d"];
idsF.forEach((id) => open(free, id));
free.setSplitMode(2);
check("free -> split 2 spreads existing cards", regions(free, idsF) === "0101");
free.setSplitMode(4);
check("split 4 keeps remembered regions", regions(free, idsF) === "0101");
open(free, "e");
open(free, "g");
check("new cards fill the empty regions", regions(free, ["e", "g"]) === "23");
free.setSplitMode(2);
const all = [...idsF, "e", "g"];
check("split 4 -> 2 rebalances cards whose region is gone", regions(free, all) === "010101");
const slots = new Set(all.map((id) => `${free.getViewRegion(id)}:${host3.querySelector(`.stage-card[data-view-id="${id}"]`).style.top}`));
check("no two cards share a spot after a layout switch", slots.size === all.length);

done("ALL_PLACEMENT_CONTRACTS_OK");
"""

_NOTE_MEDIA_SCRIPT = _PRELUDE + r"""
const stage = createStage(document.getElementById("host"));
const events = [];
stage.onEvent((e) => { if (e.type === "close" || e.type === "close_undoable") events.push(e); });
const closeBtn = (id) => card(id).querySelector(".stage-card-close");
const closeAndCollect = (id) => { events.length = 0; closeBtn(id).click(); return events.map((e) => e.type).join(); };

// --- Note: label, and Undo only for a note that has text ---------------------
stage.showView({ kind: "markdown", id: "n1", title: "Note", markdown: "", editable: true });
check("the note says where its text lives", card("n1").querySelector(".stage-note-status").textContent === "Saved in this browser");
const textarea = card("n1").querySelector(".stage-note-textarea");
textarea.value = "# Draft\n\nkeep me ☕";
textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
check("closing a note with text offers Undo", closeAndCollect("n1") === "close,close_undoable");
const undo = events.find((e) => e.type === "close_undoable");
check("the Undo event carries the exact text", undo.spec.markdown === "# Draft\n\nkeep me ☕" && undo.spec.text === undo.spec.markdown);
check("the Undo event carries the id and editable spec", undo.id === "n1" && undo.spec.editable === true && undo.spec.kind === "markdown");
// re-adding that spec brings the same note (and its text) back
stage.showView(undo.spec);
check("re-adding the spec restores the text", card("n1").querySelector(".stage-note-textarea").value === "# Draft\n\nkeep me ☕");

stage.showView({ kind: "markdown", id: "n2", title: "Note", markdown: "", editable: true });
check("an empty note closes with no Undo", closeAndCollect("n2") === "close");
stage.showView({ kind: "markdown", id: "n3", title: "Note", markdown: "  \n\t", editable: true });
check("a whitespace-only note closes with no Undo", closeAndCollect("n3") === "close");
stage.showView({ kind: "markdown", id: "m1", title: "Search", markdown: "# real content" });
check("read-only markdown closes with no Undo", closeAndCollect("m1") === "close");
stage.showView({ kind: "image", id: "i1", title: "Image", url: "https://example.com/a.png" });
check("other kinds close with no Undo", closeAndCollect("i1") === "close");
events.length = 0;
stage.showView({ kind: "markdown", id: "n4", title: "Note", markdown: "x", editable: true });
stage.closeView("n4");
check("closeView itself stays silent", events.map((e) => e.type).join() === "close");

// --- media failures: same message for image and video, announced, escaped ----
stage.showView({ kind: "image", id: "i2", title: "I", url: "https://example.com/<b>x</b>.png" });
card("i2").querySelector("img").dispatchEvent(new window.Event("error"));
const imageError = card("i2").querySelector(".stage-media-error");
check("image failure is an alert with the url", imageError?.getAttribute("role") === "alert"
  && imageError.textContent.includes("Image failed to load: https://example.com/<b>x</b>.png"));
check("the url is text, not markup", imageError?.querySelector("b") === null);
stage.showView({ kind: "video", id: "v2", title: "V", url: "https://example.com/missing.mp4" });
card("v2").querySelector("video").dispatchEvent(new window.Event("error"));
const videoError = card("v2").querySelector(".stage-media-error");
check("video failure is an alert with the url", videoError?.getAttribute("role") === "alert"
  && videoError.textContent.includes("Video failed to load: https://example.com/missing.mp4"));
check("the dead player is replaced by the message", card("v2").querySelector("video") === null);

done("ALL_NOTE_MEDIA_CONTRACTS_OK");
"""


def _run_stage_script(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    shutil.copy(UI_JS / "stage.js", tmp_path / "stage.js")
    (tmp_path / "check_stage.mjs").write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)
    return subprocess.run(
        [BUN, str(tmp_path / "check_stage.mjs")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(tmp_path),
        env=env,
    )


@pytestmark_dom
def test_split_placement_under_real_dom(tmp_path: Path) -> None:
    """New cards spread over the regions, cascade, and survive a reload / layout switch."""
    result = _run_stage_script(tmp_path, _PLACEMENT_SCRIPT)
    assert result.returncode == 0, f"placement contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_PLACEMENT_CONTRACTS_OK" in result.stdout


@pytestmark_dom
def test_note_undo_and_media_failures_under_real_dom(tmp_path: Path) -> None:
    """Only a note with text offers Undo; failed images and videos explain themselves."""
    result = _run_stage_script(tmp_path, _NOTE_MEDIA_SCRIPT)
    assert result.returncode == 0, f"note/media contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_NOTE_MEDIA_CONTRACTS_OK" in result.stdout
