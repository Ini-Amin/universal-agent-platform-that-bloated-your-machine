"""Tests for split-pane mode and the canvas WebSocket channel (stage.js).

Split pane mode: free canvas is the default; the toolbar switches the stage to
2 or 4 regions, cards move across regions, dividers drag the region ratio, and
the layout (mode, ratios, per-view region + position) survives a reload.

Canvas WebSocket: stage.js opens one socket per tab to /ws/canvas, reports its
view set with a debounced `hello` snapshot (connect / structural / focus /
editor file changes), and obeys open_file / add_view / focus_view commands with
an automatic reconnect + backoff when the server reloads.

"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"
BUN = shutil.which("bun")

# ---------------------------------------------------------------------------
# Behavioral contracts (real stage.js under a real DOM + fake socket)
# ---------------------------------------------------------------------------

def _find_linkedom_root() -> Path | None:
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

_SPLIT_SCRIPT = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML(
  "<!doctype html><html><body><div id='host'></div><div id='host2'></div></body></html>"
);
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;

// In-memory localStorage so persistence is actually exercised (linkedom has none)
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

// Fake WebSocket: records frames and instances so reconnect can be observed
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }
  send(s) { this.sent.push(s); }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}
FakeWebSocket.instances = [];
globalThis.WebSocket = FakeWebSocket;

const { createStage } = await import("./stage.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const card = (id) => document.querySelector(`.stage-card[data-view-id="${id}"]`);
const lastSent = (ws) => JSON.parse(ws.sent[ws.sent.length - 1]);
const waitHello = async (ws, predicate) => {
  for (let i = 0; i < 20; i += 1) {
    if (ws.sent.length && predicate(lastSent(ws))) return lastSent(ws);
    await sleep(25);
  }
  return null;
};

const stage = createStage(document.getElementById("host"));
const grid = document.querySelector("#host .stage-grid");
// the owning page wires the socket, never the constructor
check("no socket from the constructor", FakeWebSocket.instances.length === 0);
stage.connectCanvas();
check("one canvas socket per tab", FakeWebSocket.instances.length === 1);
check("idempotent connect", (stage.connectCanvas(), FakeWebSocket.instances.length === 1));
check("socket path", FakeWebSocket.instances[0].url.endsWith("/ws/canvas"));
check("token absent -> no query param", !FakeWebSocket.instances[0].url.includes("token="));

// --- free canvas is the default -------------------------------------------
check("default mode is free", stage.getSplitMode() === 0);
check("no regions in free mode", document.querySelectorAll(".stage-region").length === 0);
check("null region outside split", stage.getViewRegion("nope") === null);

const ids = ["a", "b", "c"].map((id) =>
  stage.showView({ kind: "html", id, title: id.toUpperCase(), html: `<b>${id}</b>` })
);
check("views opened", ids.length === 3 && stage.listViews().length === 3);

// --- switching to split 2 --------------------------------------------------
stage.setSplitMode(2);
check("mode switched", stage.getSplitMode() === 2);
check("two regions", document.querySelectorAll(".stage-region").length === 2);
check("one divider", document.querySelectorAll(".stage-split-divider").length === 1);
check("grid has split classes", grid.classList.contains("stage-grid-split") && grid.classList.contains("stage-grid-split-2"));
check("cards moved into region 0", Array.from(document.querySelectorAll(".stage-card"))
  .every((el) => el.parentElement === document.querySelector('.stage-region[data-region="0"]')));
check("region labels rendered", document.querySelectorAll(".stage-region-label").length === 2);
check("zoom is inert in split", (stage.zoomIn(), grid.style.transform === "none"));
check("ratio var default", grid.style.getPropertyValue("--split-x") === "0.5fr");

// --- moving a card across regions ------------------------------------------
const regionEvents = [];
stage.onEvent((e) => { if (e.type === "region") regionEvents.push(e); });
check("setViewRegion ok", stage.setViewRegion("a", 1) === true);
check("card re-parented", card("a").parentElement === document.querySelector('.stage-region[data-region="1"]'));
check("region event", regionEvents.length === 1 && regionEvents[0].id === "a" && regionEvents[0].region === 1);
check("getViewRegion follows", stage.getViewRegion("a") === 1 && stage.getViewRegion("b") === 0);
check("region persisted", store.get("uap.stage.split.region.a") === "1");
const posA = JSON.parse(store.get("uap.stage.split.pos.a"));
check("split position persisted", typeof posA.x === "number" && typeof posA.y === "number");

// closeView forgets the region assignment
stage.closeView("c");
check("close removes card", card("c") === null && !store.has("uap.stage.split.region.c"));

// --- draggable ratio -------------------------------------------------------
stage.setSplitRatio("x", 0.25);
check("ratio var updated", grid.style.getPropertyValue("--split-x") === "0.25fr");
check("ratio persisted", JSON.parse(store.get("uap.stage.split.ratios")).x === 0.25);
stage.setSplitRatio("x", 0); // clamped, never a collapsed region
check("ratio clamped", grid.style.getPropertyValue("--split-x") === "0.15fr");

// --- back to free canvas ---------------------------------------------------
stage.setSplitMode(0);
check("free again", stage.getSplitMode() === 0);
check("regions removed", document.querySelectorAll(".stage-region").length === 0);
check("cards back on grid", card("a").parentElement === grid);
check("transform restored", grid.style.transform !== "none");

// --- reload restores the layout -------------------------------------------
stage.setSplitMode(4);
stage.setSplitRatio("y", 0.3);
stage.disconnectCanvas();
const reloaded = createStage(document.getElementById("host2"));
check("mode restored", reloaded.getSplitMode() === 4);
check("four regions restored", document.querySelectorAll("#host2 .stage-region").length === 4);
check("two dividers restored", document.querySelectorAll("#host2 .stage-split-divider").length === 2);
const grid2 = document.getElementById("host2").querySelector(".stage-grid");
check("y ratio restored", grid2.style.getPropertyValue("--split-y") === "0.3fr");
check("x ratio restored", grid2.style.getPropertyValue("--split-x") === "0.15fr"); // the clamped value sticks
reloaded.showView({ kind: "html", id: "a", title: "A", html: "<b>a</b>" });
const reloadedCard = document.querySelector('#host2 .stage-card[data-view-id="a"]');
check("card follows saved region", reloadedCard?.parentElement.getAttribute("data-region") === "1");

// --- hello snapshot over the canvas socket ---------------------------------
stage.connectCanvas(); // the original stage's socket, after the reload detour
const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
ws.readyState = 1;
ws.onopen();
let hello = lastSent(ws);
check("hello on connect", hello.type === "hello" && Array.isArray(hello.views) && "focused" in hello);
check("hello lists views", hello.views.some((v) => v.id === "a" && v.type === "html" && v.title === "A"));

stage.showView({ kind: "editor", id: "file:one.txt", title: "one.txt", path: "notes/one.txt", root: "r1" });
hello = await waitHello(ws, (m) => m.views?.some((v) => v.path === "notes/one.txt"));
check("hello after structural change", Boolean(hello));
const editorView = hello.views.find((v) => v.id === "file:one.txt");
check("hello carries open path", editorView.path === "notes/one.txt");
check("hello carries open root", editorView.root === "r1");

check("focusView sets focused", stage.focusView("a") === true && stage.getFocusedViewId() === "a");
hello = await waitHello(ws, (m) => m.focused === "a");
check("hello refreshes on focus", hello?.focused === "a");

// --- server commands obeyed ------------------------------------------------
const viewsBefore = stage.listViews().length;
stage.setCanvasFileOpener(() => "handled");
ws.onmessage({ data: JSON.stringify({ type: "open_file", path: "src/app.py", root: "r0" }) });
check("opener handled: no fallback card",
  card("file:src/app.py") === null && stage.listViews().length === viewsBefore);

stage.setCanvasFileOpener(() => false); // opener's view is hidden -> fallback
ws.onmessage({ data: JSON.stringify({ type: "open_file", path: "src/app.py" }) });
check("fallback editor card", card("file:src/app.py") !== null && card("file:src/app.py").parentElement !== null);
check("fallback path on card", Boolean(card("file:src/app.py").querySelector("textarea, .stage-editor-host")));

ws.onmessage({ data: JSON.stringify({ type: "add_view", spec: { kind: "html", id: "pushed", title: "P", html: "<i>p</i>" } }) });
check("add_view opens a card", Boolean(card("pushed")));

ws.onmessage({ data: JSON.stringify({ type: "focus_view", id: "b" }) });
check("focus_view focuses", stage.getFocusedViewId() === "b");

// unknown/garbage frames are ignored, not fatal
ws.onmessage({ data: "not json" });
ws.onmessage({ data: JSON.stringify({ type: "mystery" }) });
check("channel still usable after bad frames", stage.listViews().some((v) => v.id === "pushed"));

// --- reconnect with backoff, stopped on unload ------------------------------
ws.close(); // simulate a server reload
check("stage still usable after socket drop", stage.listViews().length > 0);
await sleep(700);
const reconnected = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
check("reconnected to /ws/canvas", reconnected !== ws && reconnected.url.endsWith("/ws/canvas"));

window.dispatchEvent(new window.Event("pagehide"));
reconnected.close();
await sleep(700);
check("page unload stops the reconnect loop",
  FakeWebSocket.instances[FakeWebSocket.instances.length - 1] === reconnected);

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_SPLIT_CONTRACTS_OK");
"""


@pytestmark_dom
def test_split_and_canvas_ws_contracts_under_real_dom(tmp_path: Path) -> None:
    """Run the real ui/js/stage.js against a linkedom DOM and assert split + WS."""
    shutil.copy(UI_JS / "stage.js", tmp_path / "stage.js")
    (tmp_path / "check_split.mjs").write_text(_SPLIT_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)

    result = subprocess.run(
        [BUN, str(tmp_path / "check_split.mjs")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, (
        f"split/canvas-ws contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ALL_SPLIT_CONTRACTS_OK" in result.stdout
