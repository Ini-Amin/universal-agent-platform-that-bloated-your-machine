"""Frontend tests for the real canvas code editor (``ui/js/editor.js``).

These run the *actual* module under a linkedom DOM with Bun (same harness the
stage contract test uses). They cover the two claims that must not be hand-waved:

* **Monaco is really wired** -- given a working AMD loader, the editor host is
  handed to ``monaco.editor.create`` and the status bar reads "Monaco".
* **The CDN failure is honest** -- given a loader that fails, the view falls
  back to a textarea AND the status bar says Monaco is unavailable. It is never
  a blank panel.

Plus the file browser and "Open in Zed" wiring against a stubbed fetch, and the
static contracts (module is served, index.html injects it, CSS exists).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app

UI_DIR = Path(__file__).resolve().parents[1] / "ui"
UI_JS = UI_DIR / "js"

BUN = shutil.which("bun")


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
    for base in (
        Path.home() / ".npm-global" / "lib" / "node_modules",
        Path("/usr/lib/node_modules"),
    ):
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
    reason="needs Bun + linkedom to run ui/js/editor.js against a DOM",
)


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(runs_dir=tmp_path / "runs", run_inline=True)


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------

def test_editor_js_is_served_and_exports_the_api(client: TestClient) -> None:
    res = client.get("/js/editor.js")
    assert res.status_code == 200, res.text
    assert "export function buildEditorBody" in res.text
    assert "export async function loadMonaco" in res.text
    assert "export function monacoLanguageId" in res.text
    assert "cdn.jsdelivr.net/npm/monaco-editor" in res.text
    # The loader is fetched, never bundled.
    assert "loader.js" in res.text


def test_editor_js_has_no_bundled_monaco(client: TestClient) -> None:
    """The CDN loader is referenced by URL; Monaco itself is not inlined."""
    text = client.get("/js/editor.js").text
    # A bundled Monaco would be hundreds of KB; this file is the wiring only.
    assert len(text) < 60_000
    assert "vs/editor/editor.main" in text


def test_index_html_injects_the_editor_builder(client: TestClient) -> None:
    html = client.get("/").text
    assert "from './js/editor.js'" in html
    assert "setEditorBodyBuilder(buildEditorBody)" in html
    assert "setEditorBodyBuilder" in client.get("/js/stage.js").text


def test_app_css_defines_editor_chrome(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    for cls in (
        ".stage-editor-host",
        ".stage-editor-files",
        ".stage-editor-file-item",
        ".stage-editor-engine",
        ".stage-editor-zed-btn",
        ".stage-editor-fallback",
    ):
        assert cls in css, cls


def test_stage_js_delegates_to_injected_builder(client: TestClient) -> None:
    text = client.get("/js/stage.js").text
    assert "export function setEditorBodyBuilder" in text
    assert "_editorBodyBuilder(body, spec, emit)" in text
    # and still ships the honest textarea fallback
    assert "_buildFallbackEditorBody" in text


# ---------------------------------------------------------------------------
# Behavioral contracts under a real DOM
# ---------------------------------------------------------------------------

# Shared prelude: build a DOM, stub fetch, and provide a helper that waits for
# the editor engine to leave the "pending" state.
_PRELUDE = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML("<!doctype html><html><head></head><body><div id='host'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const fetchCalls = [];
function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    headers: { get: (h) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}
// Default fetch: workspace listing + editor status + sandbox run + open-in-zed.
globalThis.fetch = async (url, opts = {}) => {
  fetchCalls.push({ url, method: (opts.method || "GET").toUpperCase(), body: opts.body });
  if (url.startsWith("/api/workspace/files")) {
    return jsonResponse({
      path: "", root: "/ws", truncated: false,
      entries: [
        { name: "pkg", path: "pkg", is_dir: true, size: null, contained: true },
        { name: "main.py", path: "main.py", is_dir: false, size: 20, contained: true },
      ],
    });
  }
  if (url.startsWith("/api/workspace/file?path=main.py")) {
    return jsonResponse({ path: "main.py", size: 20, content: "print('hello')\n" });
  }
  if (url.startsWith("/api/workspace/file")) {
    return jsonResponse({ path: "main.py", bytes: 13 });
  }
  if (url === "/api/editor/status") {
    return jsonResponse({ available: true, binary: "zed", reason: "installed" });
  }
  if (url === "/api/editor/open") {
    return jsonResponse({ launched: true, binary: "zed", pid: 4242, path: "main.py" });
  }
  if (url === "/api/sandbox/run") {
    return jsonResponse({ stdout: "hello\n", stderr: "", exit_code: 0, duration_ms: 1, truncated: false });
  }
  return jsonResponse({}, 404);
};

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

async function waitForEngine(body, want) {
  for (let i = 0; i < 100; i++) {
    if (body.__editor.getEngine() === want) return true;
    await new Promise((r) => setTimeout(r, 20));
  }
  return false;
}
"""

# --- Monaco success path -----------------------------------------------------
_MONACO_SCRIPT = _PRELUDE + r"""
// A fake Monaco that records create()/setModelLanguage() calls.
const created = [];
const setLangs = [];
const fakeInstance = {
  getValue: () => "print('hello')\n",
  setValue: () => {},
  onDidChangeModelContent: () => {},
  onDidChangeCursorPosition: () => {},
  addCommand: () => {},
  getModel: () => ({ __model: true }),
};
window.monaco = {
  editor: {
    create: (el, opts) => { created.push({ el, opts }); return fakeInstance; },
    setModelLanguage: (m, lang) => setLangs.push(lang),
  },
  KeyMod: { CtrlCmd: 1 },
  KeyCode: { KeyS: 2, Enter: 3 },
};

const { buildEditorBody, monacoLanguageId } = await import("./editor.js");

// language mapping
check("lang python", monacoLanguageId("python") === "python");
check("lang py", monacoLanguageId("py") === "python");
check("lang JS case-insensitive", monacoLanguageId("JavaScript") === "javascript");
check("lang unknown -> plaintext", monacoLanguageId("brainfuck") === "plaintext");
check("lang null -> plaintext", monacoLanguageId(null) === "plaintext");

const host = document.getElementById("host");
const body = buildEditorBody(host, { language: "python", filename: "main.py", code: "print('hello')\n" }, () => {});
const ok = await waitForEngine(body, "monaco");
check("monaco engine attached", ok);
check("monaco.editor.create called", created.length === 1);
check("monaco created in the host", created[0].el.classList.contains("stage-editor-host"));
check("monaco language id passed", created[0].opts.language === "python");
check("status says Monaco", /Monaco/.test(body.querySelector(".stage-editor-engine").textContent));
check("no fallback textarea", body.querySelector(".stage-editor-textarea") === null);

// toolbar has Save/Run/Zed/Files
check("save button", body.querySelector(".stage-editor-save-btn")?.textContent.includes("Save"));
check("run button", body.querySelector(".stage-editor-run-btn")?.textContent.includes("Run"));
check("zed button", body.querySelector(".stage-editor-zed-btn") !== null);
check("files button", body.querySelector(".stage-editor-files-btn") !== null);
check("ln/col present", /Ln 1, Col 1/.test(body.querySelector(".stage-editor-pos").textContent));

// file browser rendered the workspace listing
for (let i = 0; i < 100; i++) {
  if (body.querySelectorAll(".stage-editor-file-item").length >= 2) break;
  await new Promise((r) => setTimeout(r, 20));
}
const items = body.querySelectorAll(".stage-editor-file-item");
check("file list populated", items.length === 2);
check("directory marked", items[0].classList.contains("is-dir"));
check("file not marked dir", !items[1].classList.contains("is-dir"));

// opening a file loads it into Monaco and sets the path
body.querySelectorAll(".stage-editor-file-item")[1].click();
for (let i = 0; i < 100; i++) {
  if (body.__editor.getPath() === "main.py") break;
  await new Promise((r) => setTimeout(r, 20));
}
check("file opened sets path", body.__editor.getPath() === "main.py");
check("file opened sets filename", body.__editor.getFilename() === "main.py");

// zed button becomes enabled once a path exists and status is available
for (let i = 0; i < 100; i++) {
  if (body.__editor.isZedAvailable() && !body.__editor.getZedButton().disabled) break;
  await new Promise((r) => setTimeout(r, 20));
}
check("zed available after status", body.__editor.isZedAvailable() === true);
check("zed button enabled with a path", body.__editor.getZedButton().disabled === false);

// clicking Zed issues POST /api/editor/open with the path
body.__editor.getZedButton().click();
for (let i = 0; i < 100; i++) {
  if (fetchCalls.some((c) => c.url === "/api/editor/open")) break;
  await new Promise((r) => setTimeout(r, 20));
}
const openCall = fetchCalls.find((c) => c.url === "/api/editor/open");
check("zed POST made", !!openCall);
check("zed POST is POST", openCall && openCall.method === "POST");
check("zed POST body has path", openCall && JSON.parse(openCall.body).path === "main.py");

// save issues POST /api/workspace/file
await body.__editor.save();
const saveCall = fetchCalls.find((c) => c.method === "POST" && c.url === "/api/workspace/file");
check("save POST made", !!saveCall);

// run issues POST /api/sandbox/run and shows output
await body.__editor.run();
const runCall = fetchCalls.find((c) => c.url === "/api/sandbox/run");
check("run POST made", !!runCall);
check("output drawer visible", body.querySelector(".stage-editor-output-drawer").classList.contains("is-visible"));
check("output shows stdout", /hello/.test(body.querySelector(".stage-editor-output-content").textContent));

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_EDITOR_MONACO_OK");
"""

# --- CDN-blocked fallback path ----------------------------------------------
_FALLBACK_SCRIPT = _PRELUDE + r"""
// No window.monaco; the AMD loader exists but editor.main fails to load --
// this is what an unreachable/blocked CDN looks like to the module.
delete window.monaco;
window.require = function (deps, ok, err) { err(new Error("network blocked")); };
window.require.config = function () {};

const { buildEditorBody } = await import("./editor.js");
const host = document.getElementById("host");
const body = buildEditorBody(host, { language: "python", filename: "main.py", code: "print('x')\n" }, () => {});

const ok = await waitForEngine(body, "textarea");
check("textarea fallback attached", ok);
check("textarea exists", body.querySelector(".stage-editor-textarea") !== null);
check("gutter exists", body.querySelector(".stage-editor-gutter") !== null);
check("no monaco host content", body.querySelector(".monaco-editor") === null);
const engineText = body.querySelector(".stage-editor-engine").textContent;
check("engine badge says Plain textarea", /textarea/i.test(engineText));
const status = body.querySelector(".stage-editor-status-msg").textContent;
check("status bar explains the fallback", /Monaco unavailable/i.test(status));
check("status mentions a reason", /unavailable . (network blocked|CDN)/i.test(status) || /network blocked/i.test(status));

// Save/Run still work in fallback mode. Open a real file first so Save writes
// to disk (an untitled buffer honestly falls back to a download).
for (let i = 0; i < 100; i++) {
  if (body.querySelectorAll(".stage-editor-file-item").length >= 2) break;
  await new Promise((r) => setTimeout(r, 20));
}
body.querySelectorAll(".stage-editor-file-item")[1].click();
for (let i = 0; i < 100; i++) {
  if (body.__editor.getPath() === "main.py") break;
  await new Promise((r) => setTimeout(r, 20));
}
check("fallback can open a file", body.__editor.getPath() === "main.py");
await body.__editor.save();
check("save works in fallback", fetchCalls.some((c) => c.url === "/api/workspace/file"));
await body.__editor.run();
check("run works in fallback", fetchCalls.some((c) => c.url === "/api/sandbox/run"));

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_EDITOR_FALLBACK_OK");
"""


def _run_bun_script(tmp_path: Path, script: str, marker: str) -> None:
    shutil.copy(UI_JS / "editor.js", tmp_path / "editor.js")
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)
    result = subprocess.run(
        [BUN, str(tmp_path / "check.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, (
        f"editor contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker in result.stdout


@pytestmark_dom
def test_editor_monaco_path_under_real_dom(tmp_path: Path) -> None:
    """Given a working loader, Monaco is created and every control is wired."""
    _run_bun_script(tmp_path, _MONACO_SCRIPT, "ALL_EDITOR_MONACO_OK")


@pytestmark_dom
def test_editor_cdn_blocked_falls_back_honestly(tmp_path: Path) -> None:
    """Given a failing loader, the textarea fallback says why -- never blank."""
    _run_bun_script(tmp_path, _FALLBACK_SCRIPT, "ALL_EDITOR_FALLBACK_OK")
