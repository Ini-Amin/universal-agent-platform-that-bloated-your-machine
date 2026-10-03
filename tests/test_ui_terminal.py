"""Tests for the Terminal surface of the canvas stage (ui/js/stage.js).

The terminal must never present mock data as real, must say *why* it cannot
connect, and must release its server session when its card goes away:

* a dead socket disables the input, offers **Reconnect** and prints nothing a
  shell did not print (the old client-side mock answered ``rm -rf /x``);
* the opt-in API token rides ``?token=`` like the canvas socket, and a close
  code/reason is shown in plain words (not "no terminal backend is configured");
* every path that drops a card calls ``body.__dispose`` so its WebSocket is
  closed (the server allows only five concurrent sessions);
* the header and the Problems tab claim only what is known.

Static contracts on the served assets always run. The behavioural contracts run
the REAL ``ui/js/stage.js`` under Bun with a linkedom DOM and a fake WebSocket
(skipped cleanly when Bun or linkedom is unavailable, like the other UI tests).
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

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"
BUN = shutil.which("bun")


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


def _block(js: str, header: str, end: str) -> str:
    """Source from ``header`` up to the first following ``end`` (a closing brace line)."""
    start = js.index(header)
    return js[start : js.index(end, start)]


# ---------------------------------------------------------------------------
# Static contracts
# ---------------------------------------------------------------------------

def test_terminal_has_no_client_side_mock(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    for fake in (
        "command executed",
        "[shell]",
        "no terminal backend is configured",
        "route does not exist",
        "No problems detected",
        "~/agent",
    ):
        assert fake not in js, f"stage.js still contains {fake!r}"

    terminal = _block(js, "export function buildTerminalBody(", "\n}\n")
    # one submit handler, speaking the documented /ws/terminal protocol
    assert terminal.count("addEventListener('submit'") == 1
    assert "type: 'stdin'" in terminal
    assert "type: 'input'" not in terminal
    # a dead socket is recoverable, not silently faked
    assert "Reconnect" in terminal
    assert "inputEl.disabled" in terminal


def test_terminal_socket_url_carries_the_api_token(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    urls = _block(js, "function terminalSocketUrls(", "\n}\n")
    assert "terminalToken()" in urls
    assert "searchParams.set('token'" in urls
    # the token only goes to the page's own server
    assert "u.host !== host" in urls
    assert "uap_api_token" in _block(js, "function terminalToken(", "\n}\n")


def test_every_card_removal_path_calls_the_dispose_hook(client: TestClient) -> None:
    js = client.get("/js/stage.js").text
    terminal = _block(js, "export function buildTerminalBody(", "\n}\n")
    assert "body.__dispose" in terminal

    assert "function disposeCard(" in js
    assert "body.__dispose" in _block(js, "function disposeCard(", "\n  }\n")
    for header in ("function closeView(", "function clear(", "function showView("):
        body = _block(js, header, "\n  }\n")
        assert "disposeCard(" in body, f"{header.strip()} does not dispose the card body"


def test_terminal_css_styles_disabled_controls_and_reconnect(client: TestClient) -> None:
    css = client.get("/css/app.css").text
    assert ".stage-terminal-reconnect-btn" in css
    assert ".stage-terminal-input:disabled" in css


# ---------------------------------------------------------------------------
# Behavioural contracts (real stage.js under a real DOM + fake socket)
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

const { window, document } = parseHTML("<!doctype html><html><body><div id='host'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;

const setLocation = (loc) => Object.defineProperty(window, "location", { value: loc, configurable: true });
setLocation({ protocol: "http:", host: "127.0.0.1:8090", href: "http://127.0.0.1:8090/" });

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

// Fake WebSocket: the test plays the server/network through open/receive/drop.
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }
  send(s) { this.sent.push(JSON.parse(s)); }
  close() { this.closed = true; this.readyState = 3; }
  open() { this.readyState = 1; if (this.onopen) this.onopen({}); }
  receive(frame) { if (this.onmessage) this.onmessage({ data: typeof frame === "string" ? frame : JSON.stringify(frame) }); }
  drop(code, reason = "") { this.readyState = 3; if (this.onclose) this.onclose({ code, reason }); }
}
FakeWebSocket.instances = [];
globalThis.WebSocket = FakeWebSocket;
const lastSocket = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

const { createStage } = await import("./stage.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const stage = createStage(document.getElementById("host"));
const events = [];
stage.onEvent((e) => events.push(e));

const card = (id) => document.querySelector(`.stage-card[data-view-id="${id}"]`);
const q = (id, sel) => card(id).querySelector(sel);
const text = (id, sel) => q(id, sel).textContent;
const buffer = (id) => text(id, ".stage-terminal-buffer");
const status = (id) => text(id, ".stage-terminal-status");
const inputDisabled = (id) => q(id, ".stage-terminal-input").disabled === true;
const sendDisabled = (id) => q(id, ".stage-terminal-send-btn").disabled === true;
const reconnectShown = (id) => q(id, ".stage-terminal-reconnect-btn").style.display !== "none";
// Force a submit even when the input is disabled: the handler itself must refuse.
const submit = (id, command) => {
  q(id, ".stage-terminal-input").value = command;
  q(id, ".stage-terminal-input-bar").dispatchEvent(new window.Event("submit", { cancelable: true, bubbles: true }));
};
const openTerminal = (id, extra = {}) => {
  stage.showView({ kind: "terminal", id, title: id, ...extra });
  return lastSocket();
};

function finish(okMarker) {
  if (failures.length) {
    console.error("FAILURES: " + failures.join(", "));
    process.exit(1);
  }
  console.log(okMarker);
}
"""

_HONEST_SESSION_SCRIPT = _PRELUDE + r"""
// --- connect, run, exit ----------------------------------------------------
const ws1 = openTerminal("t1");
check("socket opens at /ws/terminal", ws1.url === "ws://127.0.0.1:8090/ws/terminal");
check("no token in the url when none is stored", !ws1.url.includes("token="));
check("connecting: status", status("t1") === "Connecting...");
check("connecting: input disabled", inputDisabled("t1") && sendDisabled("t1"));
check("connecting: no reconnect button yet", !reconnectShown("t1"));
ws1.open();
check("connected: status", status("t1") === "Connected");
check("connected: input enabled", !inputDisabled("t1") && !sendDisabled("t1"));
check("connected: reconnect hidden", !reconnectShown("t1"));

submit("t1", "echo hi");
check("command is sent as a stdin frame", JSON.stringify(ws1.sent) === JSON.stringify([{ type: "stdin", data: "echo hi\n" }]));
ws1.receive({ type: "stdout", data: "hi\n" });
check("server stdout is shown", buffer("t1").includes("$ echo hi") && buffer("t1").includes("hi\n"));
const beforeControl = buffer("t1");
ws1.receive({ type: "stdout", data: "" });
ws1.receive({ type: "pong" });
check("control frames print no raw JSON", buffer("t1") === beforeControl);

ws1.receive({ type: "stdout", data: "exit\n" });
ws1.drop(1000, "session ended");
check("exit: status says the session ended", status("t1") === "Session ended");
check("exit: input and send are disabled", inputDisabled("t1") && sendDisabled("t1"));
check("exit: reconnect is offered", reconnectShown("t1"));
check("exit: the close is recorded in the buffer", buffer("t1").includes("[terminal] Session ended"));
check("exit: ended session emits terminal_disconnect", events.some((e) => e.type === "terminal_disconnect" && e.id === "t1" && e.code === 1000));

// --- nothing is faked while disconnected -----------------------------------
const frozen = buffer("t1");
const sentBefore = ws1.sent.length;
submit("t1", "rm -rf /x");
submit("t1", "pwd");
submit("t1", "echo nope");
check("no fake output after the socket died", buffer("t1") === frozen);
check("no mock strings anywhere", !/command executed|\[shell\]|no terminal backend/.test(buffer("t1")));
check("nothing is sent on a dead socket", ws1.sent.length === sentBefore);

// --- reconnect opens a fresh session ---------------------------------------
q("t1", ".stage-terminal-reconnect-btn").click();
const ws2 = lastSocket();
check("reconnect opens a fresh socket", FakeWebSocket.instances.length === 2 && ws2 !== ws1);
check("reconnect: status is connecting", status("t1") === "Connecting..." && inputDisabled("t1"));
check("reconnect: button hides while connecting", !reconnectShown("t1"));
ws2.open();
check("reconnect: connected again", status("t1") === "Connected" && !inputDisabled("t1"));
submit("t1", "echo again");
check("reconnect: commands use the new socket only", ws2.sent.length === 1 && ws2.sent[0].data === "echo again\n" && ws1.sent.length === sentBefore);
ws2.receive({ type: "stdout", data: "again\n" });
check("reconnect: new output shown", buffer("t1").endsWith("again\n"));

// --- nothing to connect to at all ------------------------------------------
const RealSocket = globalThis.WebSocket;
globalThis.WebSocket = undefined;
stage.showView({ kind: "terminal", id: "nows" });
check("no WebSocket support is stated plainly", /WebSocket is not available/.test(status("nows")) && inputDisabled("nows"));
globalThis.WebSocket = class { constructor() { throw new Error("bad url"); } };
stage.showView({ kind: "terminal", id: "badurl" });
check("a constructor failure is reported", status("badurl") === "Cannot open the terminal connection: bad url" && inputDisabled("badurl") && reconnectShown("badurl"));
globalThis.WebSocket = RealSocket;

finish("ALL_TERMINAL_SESSION_CONTRACTS_OK");
"""

_DIAGNOSIS_SCRIPT = _PRELUDE + r"""
// --- close codes are explained in plain words ------------------------------
const cases = [
  ["never opened (offline / refused)", false, 1006, "", "Cannot reach the server"],
  ["opened, then dropped", true, 1006, "", "Connection lost"],
  ["unauthorised", true, 1008, "unauthorized", "Not authorised: set the API token"],
  ["too many sessions", true, 1008, "too many concurrent terminal sessions", "Too many concurrent terminal sessions"],
  ["server restart", true, 1012, "", "The server is shutting down or restarting"],
  ["unknown code", true, 4001, "odd", "Disconnected (code 4001: odd)"],
];
cases.forEach(([name, opens, code, reason, expected], i) => {
  const id = `case-${i}`;
  const ws = openTerminal(id);
  if (opens) ws.open();
  ws.drop(code, reason);
  check(`${name}: status`, status(id).includes(expected));
  check(`${name}: recorded in the buffer`, buffer(id).includes(`[terminal] ${expected}`));
  check(`${name}: not blamed on a missing backend`, !/no terminal backend|route does not exist/.test(status(id) + buffer(id)));
  check(`${name}: input disabled`, inputDisabled(id) && sendDisabled(id));
  check(`${name}: reconnect offered`, reconnectShown(id));
  check(`${name}: terminal_error carries the code`, events.some((e) => e.type === "terminal_error" && e.id === id && e.code === code));
});
check("unauthorised: hints how to set the token", buffer("case-2").includes("?token=") && buffer("case-2").includes("uap_api_token"));

// --- the API token rides ?token= (and only to our own server) ---------------
localStorage.setItem("uap_api_token", "s3cret&x=1");
const wsTok = openTerminal("tok");
check("token is appended and encoded", wsTok.url === "ws://127.0.0.1:8090/ws/terminal?token=s3cret%26x%3D1");
wsTok.open();
check("the token never reaches stage events", !JSON.stringify(events).includes("s3cret"));
check("the connect event carries the token-free url", events.some((e) => e.type === "terminal_connect" && e.id === "tok" && e.wsUrl === "ws://127.0.0.1:8090/ws/terminal"));
check("a foreign wsUrl never receives the token", openTerminal("foreign", { wsUrl: "ws://other.example/ws/terminal" }).url === "ws://other.example/ws/terminal");
check("a same-host custom wsUrl does", openTerminal("same", { wsUrl: "ws://127.0.0.1:8090/custom" }).url === "ws://127.0.0.1:8090/custom?token=s3cret%26x%3D1");
check("an explicit token in wsUrl is left alone", openTerminal("explicit", { wsUrl: "ws://127.0.0.1:8090/ws/terminal?token=mine" }).url === "ws://127.0.0.1:8090/ws/terminal?token=mine");
setLocation({ protocol: "https:", host: "uap.example", href: "https://uap.example/" });
check("https pages use wss", openTerminal("secure").url === "wss://uap.example/ws/terminal?token=s3cret%26x%3D1");
setLocation({ protocol: "http:", host: "127.0.0.1:8090", href: "http://127.0.0.1:8090/" });

// Reconnect re-reads the token, so setting it after a 1008 recovers.
localStorage.removeItem("uap_api_token");
const wsNoTok = openTerminal("late");
check("no stored token: no query string", wsNoTok.url === "ws://127.0.0.1:8090/ws/terminal");
wsNoTok.open();
wsNoTok.drop(1008, "unauthorized");
localStorage.setItem("uap_api_token", "fixed");
q("late", ".stage-terminal-reconnect-btn").click();
check("reconnect picks up a token set after the failure", lastSocket().url === "ws://127.0.0.1:8090/ws/terminal?token=fixed");

finish("ALL_TERMINAL_DIAGNOSIS_CONTRACTS_OK");
"""

_DISPOSE_SCRIPT = _PRELUDE + r"""
// --- closing a card closes its socket --------------------------------------
const wsA = openTerminal("a");
wsA.open();
check("a live socket is open before the card closes", wsA.closed === false);
stage.closeView("a");
check("closeView closes the terminal socket", wsA.closed === true);

// '+' tabs share the one socket: no extra socket, and closing still releases it
const wsB = openTerminal("b");
wsB.open();
const socketsBefore = FakeWebSocket.instances.length;
q("b", ".stage-terminal-tab-plus").click();
q("b", ".stage-terminal-tab-plus").click();
check("'+' adds shell tabs", [...card("b").querySelectorAll(".stage-terminal-tab")].some((t) => t.textContent === "Shell 3"));
check("'+' tabs reuse the one socket", FakeWebSocket.instances.length === socketsBefore);
stage.closeView("b");
check("closing after '+' closes the socket", wsB.closed === true);

// a card closed mid-handshake is closed as soon as it opens, never before
const wsC = openTerminal("c");
const statusC = q("c", ".stage-terminal-status");
stage.closeView("c");
check("a connecting socket is not closed mid-handshake", wsC.closed === false);
wsC.open();
check("...it is closed the moment it opens", wsC.closed === true);
check("...and that late open does not touch the removed card", statusC.textContent === "Connecting...");

// a replaced terminal releases the old socket before the new one connects
const wsD1 = openTerminal("d");
wsD1.open();
const wsD2 = openTerminal("d", { title: "again" });
check("showView on an existing id closes the old socket", wsD1.closed === true && wsD2 !== wsD1 && wsD2.closed === false);
check("...and leaves exactly one card", document.querySelectorAll('.stage-card[data-view-id="d"]').length === 1);

// clear() closes every terminal
wsD2.open();
const wsE = openTerminal("e");
wsE.open();
stage.clear();
check("clear() closes every terminal socket", wsD2.closed === true && wsE.closed === true);

// Reconnect after a close does not resurrect a disposed card's socket
const wsF = openTerminal("f");
wsF.open();
const btnF = q("f", ".stage-terminal-reconnect-btn");
wsF.drop(1000, "session ended");
stage.closeView("f");
const socketsAfterClose = FakeWebSocket.instances.length;
btnF.click();
check("a removed card cannot reconnect", FakeWebSocket.instances.length === socketsAfterClose);

// the hook is generic: any kind may register __dispose and closeView calls it
let disposed = 0;
stage.showView({ kind: "html", id: "h1", html: "<b>x</b>" });
card("h1").querySelector(".stage-card-body").__dispose = () => { disposed += 1; };
stage.closeView("h1");
check("closeView calls __dispose for any kind", disposed === 1);
stage.showView({ kind: "html", id: "h2", html: "x" });
card("h2").querySelector(".stage-card-body").__dispose = () => { throw new Error("boom"); };
const realError = console.error;
console.error = () => {};
const closedDespiteError = stage.closeView("h2") === true && card("h2") === null;
console.error = realError;
check("a throwing disposer cannot keep a card open", closedDespiteError);

finish("ALL_TERMINAL_DISPOSE_CONTRACTS_OK");
"""

_HEADER_SCRIPT = _PRELUDE + r"""
openTerminal("hdr");
check("the header does not claim a path it does not know", text("hdr", ".stage-terminal-cwd") === "workspace");
openTerminal("hdr2", { cwd: "/srv/app" });
check("an explicit cwd is shown as given", text("hdr2", ".stage-terminal-cwd") === "/srv/app");

const problems = text("hdr", ".stage-terminal-problems");
check("the Problems tab says it is not available", /Not available/.test(problems));
check("the Problems tab never reports success", !/No problems|0 errors|0 warnings|\u2713/.test(problems));
check("the Problems tab has no success markup", q("hdr", ".stage-terminal-problems").children.length === 0);

finish("ALL_TERMINAL_HEADER_CONTRACTS_OK");
"""


def _run_under_bun(tmp_path: Path, script: str, stage_js: Path = UI_JS / "stage.js") -> subprocess.CompletedProcess[str]:
    shutil.copy(stage_js, tmp_path / "stage.js")
    (tmp_path / "check_terminal.mjs").write_text(script, encoding="utf-8")
    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)
    return subprocess.run(
        [BUN, str(tmp_path / "check_terminal.mjs")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(tmp_path),
        env=env,
    )


@pytestmark_dom
@pytest.mark.parametrize(
    ("script", "marker"),
    [
        pytest.param(_HONEST_SESSION_SCRIPT, "ALL_TERMINAL_SESSION_CONTRACTS_OK", id="honest-session"),
        pytest.param(_DIAGNOSIS_SCRIPT, "ALL_TERMINAL_DIAGNOSIS_CONTRACTS_OK", id="diagnosis-and-token"),
        pytest.param(_DISPOSE_SCRIPT, "ALL_TERMINAL_DISPOSE_CONTRACTS_OK", id="dispose"),
        pytest.param(_HEADER_SCRIPT, "ALL_TERMINAL_HEADER_CONTRACTS_OK", id="header-and-problems"),
    ],
)
def test_terminal_contracts_under_real_dom(tmp_path: Path, script: str, marker: str) -> None:
    """Run the real ui/js/stage.js against a linkedom DOM and a fake WebSocket."""
    result = _run_under_bun(tmp_path, script)
    assert result.returncode == 0, (
        f"terminal contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker in result.stdout
