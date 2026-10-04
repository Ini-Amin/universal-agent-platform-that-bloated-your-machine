"""Code editor surface: layout, honest errors, and live-buffer actions.

Covers the fixes made to ``ui/js/editor.js``, ``ui/js/stage.js``,
``ui/css/app.css`` and ``ui/index.html`` after the editor audit:

* Monaco is laid out by its host (``automaticLayout``) and keeps a usable
  minimum height -- the card body scrolls instead of squashing it, the toolbar
  is one row, the file tree starts collapsed in narrow cards, and the Run
  output overlays the editor instead of resizing it.
* The card header's Copy / Download read the *live* editor buffer.
* Run sends the auth header, shows stdout/stderr readably and only reports
  success for exit status 0.
* Failures reach the toast system (``error`` events) instead of ``alert()``.
* The global Ctrl+S / Ctrl+Enter shortcuts never hijack typing in a field.

Static contracts run everywhere. Behavioural ones run the real modules under
linkedom with Bun (skipped when missing); the shortcut guard is extracted from
the served ``index.html`` and run with Node.
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
    reason="needs Bun + linkedom to run the editor modules against a DOM",
)
pytestmark_node = pytest.mark.skipif(NODE is None, reason="needs Node to run the shortcut guard")


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


def _css_rule(css: str, selector: str) -> dict[str, str]:
    """Merged declarations of every flat rule whose selector list contains ``selector``."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    decls: dict[str, str] = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            for decl in m.group(2).split(";"):
                if ":" in decl:
                    key, value = decl.split(":", 1)
                    decls[key.strip()] = value.strip()
    return decls


def _px(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", value.strip())
    assert m, f"expected a px length, got {value!r}"
    return float(m.group(1))


def _def11_block(html: str) -> str:
    """The global Ctrl+S / Ctrl+Enter ``keydown`` listener, verbatim from index.html."""
    m = re.search(r"// DEF-11:.*?\n    \}\);\n", html, flags=re.S)
    assert m, "DEF-11 keyboard listener block not found in index.html"
    return m.group(0)


def _run_script(runtime: str, tmp_path: Path, script: str, marker: str, files: tuple[str, ...]) -> None:
    for name in files:
        shutil.copy(UI_JS / name, tmp_path / name)
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    env = dict(os.environ)
    if _LINKEDOM_ROOT is not None:
        env["NODE_PATH"] = str(_LINKEDOM_ROOT)
    result = subprocess.run(
        [runtime, str(tmp_path / "check.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, (
        f"contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker in result.stdout


# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------


def test_monaco_is_laid_out_by_its_host(client: TestClient) -> None:
    """Monaco must follow its host box (toolbar wrap, Files toggle, window resize)."""
    js = client.get("/js/editor.js").text
    start = js.index("monaco.editor.create(editorHost, {")
    options = js[start : js.index("});", start)]
    assert "automaticLayout: true" in options


def test_editor_host_keeps_a_minimum_height_and_the_card_body_scrolls(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    host = _css_rule(css, ".stage-editor-host")
    container = _css_rule(css, ".stage-editor-container")
    assert _px(host["min-height"]) >= 160
    # The container's floor covers the host plus toolbar, tabs, breadcrumbs and
    # status bar, so a short card scrolls its body instead of squashing Monaco.
    assert _px(container["min-height"]) >= _px(host["min-height"]) + 120
    assert _css_rule(css, ".stage-card-body")["overflow"] == "auto"


def test_toolbar_is_one_row_and_output_overlays_the_editor(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    assert _css_rule(css, ".stage-editor-toolbar")["flex-wrap"] == "nowrap"
    assert _css_rule(css, ".stage-editor-btn")["white-space"] == "nowrap"
    # An in-flow drawer used to take the editor's height (down to 0px).
    drawer = _css_rule(css, ".stage-editor-output-drawer")
    assert drawer["position"] == "absolute"
    assert _css_rule(css, ".stage-editor-container")["position"] == "relative"


def test_compact_buttons_hide_labels_visually_not_from_assistive_tech(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    label = _css_rule(css, ".stage-editor-container.is-compact .stage-editor-btn-label")
    assert label, "compact mode must restyle the button labels"
    assert "display" not in label  # display:none would drop the accessible name
    assert "clip-path" in label


def test_filled_editor_toolbar_clears_the_floating_exit_button(client: TestClient) -> None:
    """In Fill mode the 'Exit fill' button (83px, 14px from the edge) sat on top of Run."""
    css = client.get("/css/app.css").text
    toolbar = _css_rule(css, ".stage-card.stage-card-filled .stage-editor-toolbar")
    assert _px(toolbar["padding-right"]) >= 97


def test_run_sends_auth_headers_and_never_dumps_raw_json(client: TestClient) -> None:
    js = client.get("/js/editor.js").text
    call = js[js.index("await fetch(endpoint, {") :][:200]
    assert "headers: _authHeaders()" in call
    assert "Exited with code" in js
    assert "Timed out" in js
    assert "Done (${" not in js  # "Done (200)" for any HTTP 200 was the old lie


def test_editor_failures_use_toasts_not_blocking_alerts(client: TestClient) -> None:
    js = client.get("/js/editor.js").text
    assert "alert(" not in js
    html = client.get("/").text
    handler = html[html.index("stage.onEvent((evt) => {") :]
    branch = handler[handler.index("evt.type === 'error' && evt.kind === 'editor'") :][:300]
    assert "showToast({ kind: 'error'" in branch


def test_status_bar_exposes_the_full_message(client: TestClient) -> None:
    js = client.get("/js/editor.js").text
    setter = js[js.index("function setStatus(msg) {") :][:140]
    assert "statusMsg.title = msg" in setter


def test_header_copy_and_download_read_the_live_editor_buffer(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    getter = js[js.index("function getViewTextContent(spec) {") :]
    editor_branch = getter[getter.index("spec.kind === 'editor'") :][:260]
    assert "liveEditor(spec)" in editor_branch
    assert "getValue()" in editor_branch


def test_global_shortcuts_ignore_text_fields(client: TestClient) -> None:
    block = _def11_block(client.get("/").text)
    for needle in ("'INPUT'", "'TEXTAREA'", "isContentEditable", "defaultPrevented"):
        assert needle in block, needle


# ---------------------------------------------------------------------------
# Behaviour: global shortcuts (Node, logic extracted from the served page)
# ---------------------------------------------------------------------------

_SHORTCUT_SCRIPT = r"""
const listeners = [];
const clicks = [];
const toasts = [];
const document = {
  addEventListener: (type, fn) => listeners.push({ type, fn }),
  querySelector: (sel) => (sel === '.stage-editor-save-btn' ? { click: () => clicks.push('save') } : null),
  getElementById: (id) => (id === 'btn-run-again' ? { click: () => clicks.push('run-again') } : null),
};
const showToast = (t) => toasts.push(t);

__BLOCK__

const handler = listeners.find((l) => l.type === 'keydown').fn;
function press(key, target, extra = {}) {
  clicks.length = 0;
  const ev = { ctrlKey: true, metaKey: false, key, target, defaultPrevented: false,
               preventDefault() { this.defaultPrevented = true; }, ...extra };
  handler(ev);
  return { clicks: [...clicks], prevented: ev.defaultPrevented };
}
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const none = (r) => r.clicks.length === 0 && !r.prevented;

for (const [name, target] of [
  ["input", { tagName: 'INPUT' }],
  ["textarea", { tagName: 'TEXTAREA' }],
  ["contenteditable", { tagName: 'DIV', isContentEditable: true }],
]) {
  check(`Ctrl+S ignored in ${name}`, none(press('s', target)));
  check(`Ctrl+Enter ignored in ${name}`, none(press('Enter', target)));
}
check("Ctrl+S already handled by a widget is not repeated",
  press('s', { tagName: 'BUTTON' }, { defaultPrevented: true }).clicks.length === 0);

const save = press('s', { tagName: 'BODY' });
check("Ctrl+S outside fields still saves once", save.clicks.join() === 'save' && save.prevented);
const again = press('Enter', { tagName: 'BODY' });
check("Ctrl+Enter outside fields still re-runs", again.clicks.join() === 'run-again' && again.prevented);
check("Cmd+S works too", press('S', { tagName: 'DIV' }, { ctrlKey: false, metaKey: true }).clicks.join() === 'save');

if (failures.length) { console.error("FAILURES: " + failures.join(", ")); process.exit(1); }
console.log("ALL_SHORTCUTS_OK");
"""


@pytestmark_node
def test_global_shortcuts_do_not_hijack_typing(client: TestClient, tmp_path: Path) -> None:
    """Ctrl+S / Ctrl+Enter in the terminal prompt or the textarea editor must be left alone."""
    block = _def11_block(client.get("/").text)
    script = _SHORTCUT_SCRIPT.replace("__BLOCK__", block)
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "check.mjs")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_SHORTCUTS_OK" in result.stdout


# ---------------------------------------------------------------------------
# Behaviour: editor.js under a real DOM (Bun + linkedom)
# ---------------------------------------------------------------------------

_EDITOR_PRELUDE = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML("<!doctype html><html><head></head><body><div id='host'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const calls = [];
const routes = {};
function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "ERR",
    headers: { get: (h) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}
// Longest matching prefix wins, so "/api/workspace/file?path=x" beats "/api/workspace/file".
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, method: (opts.method || "GET").toUpperCase(), headers: opts.headers || {}, body: opts.body });
  const hit = Object.keys(routes).filter((p) => url.startsWith(p)).sort((a, b) => b.length - a.length)[0];
  return hit ? routes[hit](url, opts) : jsonResponse({}, 404);
};

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, tries = 100) {
  for (let i = 0; i < tries; i++) { if (fn()) return true; await sleep(20); }
  return false;
}
function finish(marker) {
  if (failures.length) { console.error("FAILURES: " + failures.join(", ")); process.exit(1); }
  console.log(marker);
}
// No CDN: the textarea fallback is the engine unless a test installs window.monaco.
function blockCdn() {
  delete window.monaco;
  window.require = function (_deps, _ok, err) { err(new Error("network blocked")); };
  window.require.config = function () {};
}
"""

_RUN_SCRIPT = _EDITOR_PRELUDE + r"""
blockCdn();
store.set("uap_api_token", "s3cret");
let reply = { status: 200, body: {} };
routes["/api/sandbox/run"] = () => jsonResponse(reply.body, reply.status);
routes["/api/editor/status"] = () => jsonResponse({ available: false, reason: "no zed" });
routes["/api/workspace/"] = () => jsonResponse({ roots: [], entries: [] });

const events = [];
const { buildEditorBody } = await import("./editor.js");
const body = buildEditorBody(document.getElementById("host"), { language: "python", filename: "main.py", code: "print(1)\n" }, (e) => events.push(e));
check("textarea engine", await until(() => body.__editor.getEngine() === "textarea"));

const q = (sel) => body.querySelector(sel);
async function run(status, payload) {
  reply = { status, body: payload };
  calls.length = 0;
  events.length = 0;
  await body.__editor.run();
  return {
    status: q(".stage-editor-status-msg").textContent,
    badge: q(".stage-editor-output-status").textContent,
    badgeClass: q(".stage-editor-output-status").className,
    output: q(".stage-editor-output-content").textContent,
    call: calls.find((c) => c.url === "/api/sandbox/run"),
    runBtn: q(".stage-editor-run-btn").textContent,
  };
}

let r = await run(200, { stdout: "hello\n", stderr: "", exit_code: 0, duration_ms: 3, truncated: false });
check("run sends the bearer token", r.call && r.call.headers.Authorization === "Bearer s3cret");
check("run is a JSON POST", r.call && r.call.method === "POST" && r.call.headers["Content-Type"] === "application/json");
check("exit 0 is Done", r.status === "Done" && r.badge.includes("Done") && r.badgeClass.includes("is-ok"));
check("stdout shown", r.output.includes("hello"));
check("no raw JSON", !r.output.includes("exit_code") && !r.output.includes("{"));
check("run button restored", r.runBtn.includes("Run") && !q(".stage-editor-run-btn").disabled);
check("success is not an error event", !events.some((e) => e.type === "error"));

r = await run(200, { stdout: "partial\n", stderr: "boom\n", exit_code: 1, duration_ms: 3, truncated: false });
check("exit 1 is reported", r.status === "Exited with code 1");
check("exit 1 is a failure badge", r.badgeClass.includes("is-fail") && r.badge.includes("Exited with code 1"));
check("stdout and stderr both readable", r.output.includes("partial") && r.output.includes("stderr") && r.output.includes("boom"));
check("not Done (200)", !/Done/.test(r.status) && !r.output.includes("exit_code"));
check("run event carries the exit code", events.some((e) => e.type === "run" && e.exit_code === 1));
check("non-zero exit is not a toast-worthy app error", !events.some((e) => e.type === "error"));

r = await run(200, { stdout: "", stderr: "Sandbox limit exceeded: execution exceeded 5.0s timeout", exit_code: 124, duration_ms: 5000, truncated: false });
check("timeout is named", r.status === "Timed out" && r.badge.includes("Timed out"));
r = await run(200, { stdout: "", stderr: "", exit_code: 124, duration_ms: 3, truncated: false });
check("a program's own exit 124 is not called a timeout", r.status === "Exited with code 124");
r = await run(200, { stdout: "", stderr: "", exit_code: 0, duration_ms: 3, truncated: false });
check("empty output is said, not blank", r.status === "Done" && r.output.includes("no output"));

r = await run(401, { detail: "unauthorized" });
check("HTTP failure is not Done", r.status === "Run failed (HTTP 401)" && r.badgeClass.includes("is-fail"));
check("HTTP failure detail is readable", r.output.includes("unauthorized") && !r.output.includes("{"));
check("HTTP failure becomes an error event", events.some((e) => e.type === "error" && e.title === "Run failed" && e.message.includes("unauthorized")));

routes["/api/sandbox/run"] = () => { throw new Error("connection refused"); };
calls.length = 0; events.length = 0;
await body.__editor.run();
check("network failure is shown", q(".stage-editor-status-msg").textContent.includes("connection refused"));
check("network failure becomes an error event", events.some((e) => e.type === "error" && e.title === "Run failed"));

finish("ALL_RUN_OK");
"""

_FAILURE_SCRIPT = _EDITOR_PRELUDE + r"""
blockCdn();
const alerts = [];
window.alert = (m) => alerts.push(String(m));
routes["/api/editor/status"] = () => jsonResponse({ available: false, reason: "no zed" });
routes["/api/workspace/roots"] = (url, opts) => (opts.method === "POST"
  ? jsonResponse({ detail: "directory does not exist: '/nope'" }, 400)
  : jsonResponse({ roots: [{ id: "workspace", label: "workspace", path: "/ws" }] }));
routes["/api/workspace/files"] = () => jsonResponse({ path: "", root: "/ws", entries: [], truncated: false });
routes["/api/workspace/file?path=big.txt"] = () => jsonResponse({ detail: "file is 3145728 bytes; the editor reads at most 1048576 bytes" }, 413);
routes["/api/workspace/file?path=main.py"] = () => jsonResponse({ path: "main.py", size: 3, content: "x=1" });
routes["/api/workspace/file"] = () => jsonResponse({ detail: "could not write" }, 503);

const events = [];
const { buildEditorBody } = await import("./editor.js");
const body = buildEditorBody(document.getElementById("host"), { language: "python", filename: "main.py", code: "x=1" }, (e) => events.push(e));
check("textarea engine", await until(() => body.__editor.getEngine() === "textarea"));
const status = () => body.querySelector(".stage-editor-status-msg");
const errors = () => events.filter((e) => e.type === "error");

// open failure: full message in the tooltip + an error event for the toast
await body.__editor.openFile("big.txt");
check("open failure is in the status bar", status().textContent.includes("HTTP 413"));
check("status bar carries the full message as a tooltip", status().title === status().textContent && status().title.includes("1048576"));
check("open failure emits a titled error", errors().some((e) => e.title === "Open failed" && e.message.includes("HTTP 413")));

// save failure
await body.__editor.openFile("main.py");
events.length = 0;
await body.__editor.save();
check("save failure is in the status bar", status().textContent.includes("Save failed") && status().title.includes("HTTP 503"));
check("save failure emits a titled error", errors().some((e) => e.title === "Save failed" && e.message.includes("HTTP 503")));

// Open Folder failure used to be a blocking alert()
events.length = 0;
body.querySelector(".stage-editor-files-open-folder").click();
body.querySelector(".stage-editor-open-folder-input").value = "/nope";
body.querySelector(".stage-editor-open-folder-submit").click();
check("open-folder error arrives", await until(() => errors().length > 0));
check("open-folder failure emits a titled error", errors().some((e) => e.title === "Open folder failed" && e.message.includes("/nope")));
check("no blocking alert", alerts.length === 0);

// A failing tree listing must not hide in a collapsed file panel.
routes["/api/workspace/files"] = () => jsonResponse({ detail: "listing unavailable" }, 503);
await body.__editor.refreshFiles();
check("listing failure reaches the status bar", status().textContent.includes("Could not list workspace") && status().title.includes("HTTP 503"));

finish("ALL_FAILURES_OK");
"""

_RESPONSIVE_SCRIPT = _EDITOR_PRELUDE + r"""
// A fake Monaco records create() options; a fake ResizeObserver lets the test
// drive the card width.
const created = [];
const fakeInstance = {
  getValue: () => "x", setValue: () => {}, onDidChangeModelContent: () => {},
  onDidChangeCursorPosition: () => {}, addCommand: () => {}, getModel: () => ({}),
};
window.monaco = {
  editor: { create: (el, opts) => { created.push({ el, opts }); return fakeInstance; }, setModelLanguage: () => {} },
  KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 2, Enter: 3 },
};
const observers = [];
globalThis.ResizeObserver = class {
  constructor(cb) { this.cb = cb; observers.push(this); }
  observe() {}
  disconnect() {}
};
const fire = (width) => observers.forEach((o) => o.cb([{ contentRect: { width } }]));
routes["/api/editor/status"] = () => jsonResponse({ available: true, binary: "zed", reason: "ok" });
routes["/api/workspace/"] = () => jsonResponse({ roots: [], entries: [] });

const { buildEditorBody } = await import("./editor.js");
const body = buildEditorBody(document.getElementById("host"), { language: "python", filename: "main.py", code: "x" }, () => {});
check("monaco engine", await until(() => body.__editor.getEngine() === "monaco"));
check("monaco is created with automaticLayout", created.length === 1 && created[0].opts.automaticLayout === true);

const container = body.querySelector(".stage-editor-container");
const files = body.querySelector(".stage-editor-files");
const filesBtn = body.querySelector(".stage-editor-files-btn");
const compact = () => container.classList.contains("is-compact");
const hidden = () => files.classList.contains("is-hidden");

check("an observer watches the card", observers.length >= 1);
check("tree is visible before any measurement", !hidden() && filesBtn.classList.contains("is-active"));

fire(0);
check("a zero-width (detached) card changes nothing", !hidden() && !compact());

fire(410); // Split 2 on a 1280px laptop
check("narrow card: tree starts collapsed", hidden() && !filesBtn.classList.contains("is-active"));
check("narrow card: buttons go icon-only", compact());

filesBtn.click();
check("Files button toggles the tree back", !hidden() && filesBtn.classList.contains("is-active"));
fire(380);
check("a manual choice sticks while the card stays narrow", !hidden());
filesBtn.click();
check("Files button hides it again", hidden());

fire(540); // Free-mode default: roomy toolbar, tree still collapsed
check("540px card: labels shown, tree collapsed", !compact() && hidden());
fire(720);
check("wide card: tree is back", !hidden() && !compact());
fire(410);
check("narrowing again re-collapses", hidden() && compact());

// every toolbar button keeps its text (accessible name) next to the icon
for (const cls of ["files-btn", "zed-btn", "save-btn", "run-btn"]) {
  const btn = body.querySelector(`.stage-editor-${cls}`);
  check(`${cls} has an icon and a label`, btn && btn.querySelector(".stage-editor-btn-icon") && btn.querySelector(".stage-editor-btn-label").textContent.length > 0);
}

finish("ALL_RESPONSIVE_OK");
"""


@pytestmark_dom
def test_run_authenticates_and_reports_the_real_exit_status(tmp_path: Path) -> None:
    _run_script(BUN, tmp_path, _RUN_SCRIPT, "ALL_RUN_OK", ("editor.js",))


@pytestmark_dom
def test_failures_reach_the_toast_system_with_the_full_message(tmp_path: Path) -> None:
    _run_script(BUN, tmp_path, _FAILURE_SCRIPT, "ALL_FAILURES_OK", ("editor.js",))


@pytestmark_dom
def test_editor_adapts_to_the_card_width(tmp_path: Path) -> None:
    _run_script(BUN, tmp_path, _RESPONSIVE_SCRIPT, "ALL_RESPONSIVE_OK", ("editor.js",))


# ---------------------------------------------------------------------------
# Behaviour: the card header's Copy / Download (stage.js under linkedom)
# ---------------------------------------------------------------------------

_HEADER_SCRIPT = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML("<!doctype html><html><body><div id='host'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.HTMLElement = window.HTMLElement;
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
class FakeWebSocket { static OPEN = 1; constructor() { this.readyState = 0; } send() {} close() {} }
globalThis.WebSocket = FakeWebSocket;

const clips = [];
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async (t) => { clips.push(t); } }, platform: "test", userAgent: "test" },
  configurable: true,
});
const blobs = [];
URL.createObjectURL = (b) => { blobs.push(b); return "blob:test"; };
URL.revokeObjectURL = () => {};
const anchors = [];
const createElement = document.createElement.bind(document);
document.createElement = (tag, ...rest) => {
  const el = createElement(tag, ...rest);
  if (String(tag).toLowerCase() === "a") anchors.push(el);
  return el;
};

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const card = (id) => document.querySelector(`.stage-card[data-view-id="${id}"]`);

const { createStage, setEditorBodyBuilder } = await import("./stage.js");
const stage = createStage(document.getElementById("host"));

// 1. stage.js's own textarea editor: the template is "# template", the buffer is what was typed.
stage.showView({ id: "ed", kind: "editor", title: "Code Editor", language: "python", filename: "main.py", code: "# template\n" });
const textarea = card("ed").querySelector(".stage-editor-textarea");
check("editor card has a textarea", !!textarea);
textarea.value = "LIVE_BUFFER_CONTENT = 42";

card("ed").querySelector(".stage-card-btn-copy").click();
await sleep(30);
check("Copy puts the live buffer on the clipboard", clips[clips.length - 1] === "LIVE_BUFFER_CONTENT = 42");

card("ed").querySelector(".stage-card-btn-download").click();
await sleep(30);
check("Download saves the live buffer", blobs.length === 1 && (await blobs[blobs.length - 1].text()) === "LIVE_BUFFER_CONTENT = 42");

// 2. An editor opened on a file has no template at all; it must still offer Copy.
stage.showView({ id: "file:a.py", kind: "editor", title: "a.py", path: "a.py" });
check("file-backed editor still offers Copy", !!card("file:a.py").querySelector(".stage-card-btn-copy"));

// 3. The injected (Monaco) editor: content and filename follow the active tab.
setEditorBodyBuilder((body) => {
  body.__editor = { getValue: () => "FROM_THE_ACTIVE_TAB", getFilename: () => "live.py" };
  return body;
});
stage.showView({ id: "ed2", kind: "editor", title: "Code Editor", language: "python", filename: "main.py", code: "TEMPLATE" });
card("ed2").querySelector(".stage-card-btn-copy").click();
await sleep(30);
check("Copy follows the injected editor", clips[clips.length - 1] === "FROM_THE_ACTIVE_TAB");
card("ed2").querySelector(".stage-card-btn-download").click();
await sleep(30);
check("Download content follows the injected editor", (await blobs[blobs.length - 1].text()) === "FROM_THE_ACTIVE_TAB");
check("Download is named after the active tab", anchors[anchors.length - 1].download === "live.py");

// 4. Other kinds are untouched: a note still copies its text.
stage.showView({ id: "n", kind: "markdown", title: "Note", markdown: "hello note" });
card("n").querySelector(".stage-card-btn-copy").click();
await sleep(30);
check("markdown Copy unchanged", clips[clips.length - 1] === "hello note");

if (failures.length) { console.error("FAILURES: " + failures.join(", ")); process.exit(1); }
console.log("ALL_HEADER_OK");
"""


@pytestmark_dom
def test_header_copy_and_download_follow_the_live_buffer(tmp_path: Path) -> None:
    _run_script(BUN, tmp_path, _HEADER_SCRIPT, "ALL_HEADER_OK", ("stage.js",))
