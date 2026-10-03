"""Contract tests for the approval decision UI (PRODUCT.md: human control).

When policy blocks a side-effectful tool call the backend records a pending
approval, and ``GET /tasks/{id}`` lists it under ``pending_approvals``. The
output pane renders those as a "Needs your approval" section with Approve and
Reject buttons that call ``POST /approvals/{id}/decide``.

Three layers:

1. Backend facts the UI wording depends on (payload shape, error bodies, and
   that a decision does NOT resume or re-run anything).
2. Static contracts on the served JS assets.
3. Behaviour of the REAL ``ui/js`` modules under Bun (or Node) with a fake
   WebSocket/fetch; the DOM flow additionally needs linkedom and skips cleanly
   without it, like ``test_ui_stage.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.contracts import ApprovalState
from uap.server import create_app

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"

BUN = shutil.which("bun")
NODE = shutil.which("node")
_RUNTIME = BUN or NODE


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


def _task_with_approval(client: TestClient, app: FastAPI):
    res = client.post("/tasks", json={"input": "research database indexing strategies"})
    assert res.status_code == 200
    task_id = res.json()["task_id"]
    req = app.state.gate.request(
        task_id=task_id,
        action="execute_exploit",
        details={"tier": 3, "args": {"target": "10.0.0.1"}},
    )
    return task_id, req


# ---------------------------------------------------------------------------
# 1. Backend facts the UI relies on
# ---------------------------------------------------------------------------


def test_task_detail_carries_everything_the_approval_section_renders(
    client: TestClient, app: FastAPI
) -> None:
    task_id, req = _task_with_approval(client, app)

    (item,) = client.get(f"/tasks/{task_id}").json()["pending_approvals"]

    assert {"approval_id", "task_id", "action", "details", "state", "requested_at"} <= set(item)
    assert item["approval_id"] == req.approval_id
    assert item["state"] == "pending_approval"
    # The pane shows the tier as a badge and the args as readable JSON.
    assert item["details"] == {"tier": 3, "args": {"target": "10.0.0.1"}}
    # The pane formats it with `new Date(requested_at)`, so it must carry a zone.
    stamp = datetime.fromisoformat(item["requested_at"].replace("Z", "+00:00"))
    assert stamp.tzinfo is not None


def test_decide_errors_carry_a_detail_string_the_ui_can_show(
    client: TestClient, app: FastAPI
) -> None:
    _, req = _task_with_approval(client, app)
    url = f"/approvals/{req.approval_id}/decide"

    first = client.post(url, json={"approved": False, "decided_by": "user"})
    assert first.status_code == 200
    assert first.json()["state"] == "rejected"
    assert first.json()["decided_by"] == "user"

    again = client.post(url, json={"approved": True, "decided_by": "user"})
    assert again.status_code == 409
    assert "already decided" in again.json()["detail"]

    unknown = client.post("/approvals/nope/decide", json={"approved": True, "decided_by": "user"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "unknown approval"


def test_a_decision_is_recorded_but_does_not_resume_or_rerun_the_run(
    client: TestClient, app: FastAPI
) -> None:
    """The pane tells the user the run is not restarted by a decision.

    If the backend ever resumes or re-runs on a decision, this fails and the
    toast / section wording in ui/js/inspector.js must change with it.
    """
    task_id, req = _task_with_approval(client, app)
    status_before = client.get(f"/tasks/{task_id}").json()["status"]
    events_before = len(app.state.memory_sink.query(task_id=task_id))

    res = client.post(
        f"/approvals/{req.approval_id}/decide",
        json={"approved": True, "decided_by": "user"},
    )
    assert res.status_code == 200

    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["pending_approvals"] == []
    assert detail["status"] == status_before
    assert len(app.state.memory_sink.query(task_id=task_id)) == events_before
    assert app.state.gate.get(req.approval_id).state is ApprovalState.APPROVED


# ---------------------------------------------------------------------------
# 2. Static contracts on the served assets
# ---------------------------------------------------------------------------


def test_api_js_decide_approval_posts_the_documented_body(client: TestClient) -> None:
    js = client.get("/js/api.js").text
    assert "export async function decideApproval(approvalId, approved, decidedBy = 'user')" in js
    assert "`/approvals/${encodeURIComponent(approvalId)}/decide`" in js
    assert "JSON.stringify({ approved, decided_by: decidedBy })" in js


def test_inspector_js_wires_the_approval_section(client: TestClient) -> None:
    js = client.get("/js/inspector.js").text
    assert "decideApproval," in js and "getTask," in js
    assert "Needs your approval" in js
    assert "pending_approvals" in js
    # Every interpolated value goes through the file's escaper.
    assert "escapeHtml(action)" in js
    assert "escapeHtml(JSON.stringify(shown, null, 2))" in js
    # Buttons name the action, and nothing steals focus onto Approve.
    assert "aria-label=\"${escapeHtml(`${label} ${action}`)}\"" in js
    assert "autofocus" not in js
    # The in-flight state survives render(), which rebuilds the pane.
    assert "const deciding = new Map();" in js
    # The section is not tied to one inspector view.
    assert "renderApprovals(execState);" in js


def test_event_stream_js_refetches_on_approval_signals(client: TestClient) -> None:
    js = client.get("/js/event-stream.js").text
    assert "kind.includes('approval')" in js
    assert "newStatus === 'awaiting_approval'" in js
    assert "newStatus === 'paused'" in js
    assert "TASK_SYNC_DELAY_MS" in js
    # The refetched detail (with pending_approvals) is stored for the pane.
    assert "            task,\n" in js


def test_page_surfaces_a_run_that_needs_approval(client: TestClient) -> None:
    html = client.get("/").text
    css = client.get("/css/app.css").text
    # The pane holding Approve / Reject starts collapsed, so it is revealed when
    # an approval first appears (or flagged, if the user closed it on purpose).
    assert "waitingKey !== _approvalSeenKey" in html
    assert "revealOutput();" in html
    assert "awaiting_approval: 'Needs approval'" in html
    for selector in (".badge-awaiting_approval", ".status-dot-awaiting_approval"):
        assert selector in css


# ---------------------------------------------------------------------------
# 3. Behaviour of the real modules
# ---------------------------------------------------------------------------

_NEEDS_RUNTIME = pytest.mark.skipif(
    _RUNTIME is None, reason="needs a JS runtime (bun or node) to execute ui/js"
)


def _find_linkedom_root() -> Path | None:
    """Locate a node_modules directory containing linkedom (same idea as test_ui_stage)."""
    candidates: list[Path] = [Path(p) for p in os.environ.get("NODE_PATH", "").split(os.pathsep) if p]
    npm = shutil.which("npm")
    if npm:
        try:
            r = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                g = Path(r.stdout.strip().splitlines()[-1])
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


def _run_js(tmp_path: Path, script: str, runtime: str, env: dict | None = None) -> subprocess.CompletedProcess:
    for name in ("api.js", "state.js", "toast.js", "inspector.js", "event-stream.js"):
        shutil.copy(UI_JS / name, tmp_path / name)
    (tmp_path / "check.mjs").write_text(script, encoding="utf-8")
    return subprocess.run(
        [runtime, str(tmp_path / "check.mjs")],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(tmp_path),
        env=env,
    )


_HTML_SCRIPT = r"""
import { renderApprovalsHtml } from "./inspector.js";

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const count = (s, re) => (s.match(re) || []).length;

const base = {
  approval_id: "11111111-2222-3333-4444-555555555555",
  task_id: "t1",
  action: "execute_exploit",
  details: { tier: 3, args: { target: "10.0.0.1" } },
  state: "pending_approval",
  requested_at: "2026-10-03T12:34:56.789000Z",
};

check("empty list renders nothing", renderApprovalsHtml([]) === "");
check("non-array renders nothing", renderApprovalsHtml(null) === "");

const html = renderApprovalsHtml([base]);
check("title with count", html.includes("Needs your approval (1)"));
check("action shown", html.includes(">execute_exploit<"));
check("tier shown", html.includes("Tier 3 · side-effectful"));
check("args shown as readable json", html.includes("&quot;target&quot;: &quot;10.0.0.1&quot;"));
check("requested time shown", html.includes('<time datetime="2026-10-03T12:34:56.789000Z">Requested '));
check("approve is named after the action", html.includes('aria-label="Approve execute_exploit"'));
check("reject is named after the action", html.includes('aria-label="Reject execute_exploit"'));
check("reject precedes approve in tab order", html.indexOf('data-decision="reject"') < html.indexOf('data-decision="approve"'));
check("nothing is auto-focused", !/autofocus/i.test(html));
check("idle buttons are enabled", count(html, /\bdisabled\b/g) === 0);
check("buttons carry the approval id", html.includes('data-approval-id="11111111-2222-3333-4444-555555555555"'));

const busy = renderApprovalsHtml([base], new Map([[base.approval_id, "approve"]]));
check("both buttons disable while in flight", count(busy, /\bdisabled\b/g) === 2);
check("approve shows progress", busy.includes("Approving…") && !busy.includes("Rejecting…"));
const busyReject = renderApprovalsHtml([base], new Map([[base.approval_id, "reject"]]));
check("reject shows progress", busyReject.includes("Rejecting…") && !busyReject.includes("Approving…"));
const two = renderApprovalsHtml([base, { ...base, approval_id: "other" }], new Map([[base.approval_id, "approve"]]));
check("only the in-flight approval is disabled", count(two, /\bdisabled\b/g) === 2 && two.includes("(2)"));

const hostile = renderApprovalsHtml([{
  ...base,
  approval_id: 'id"><script>alert(3)</script>',
  action: '<img src=x onerror="alert(1)">',
  details: { tier: 3, args: { cmd: "<script>alert(2)</script>", q: '" onmouseover="x' } },
}]);
check("no raw tags from data", !/<(img|script)/i.test(hostile));
check("data is escaped", hostile.includes("&lt;img") && hostile.includes("&lt;script&gt;"));
check("attribute breakout is escaped", !hostile.includes('"><script>'));

check("empty args say so", renderApprovalsHtml([{ ...base, details: { tier: 3, args: {} } }]).includes("None recorded."));
const extra = renderApprovalsHtml([{ ...base, details: { args: { a: 1 }, note: "n" } }]);
check("other detail keys are shown", extra.includes(">Details<") && extra.includes("&quot;note&quot;"));
check("no tier, no badge", !extra.includes("Tier"));
const long = renderApprovalsHtml([{ ...base, details: { tier: 3, args: { v: "x".repeat(500) } } }]);
check("long values wrap", long.includes("overflow-wrap: anywhere") && long.includes("x".repeat(500)));
check("missing requested_at is tolerated", !renderApprovalsHtml([{ ...base, requested_at: undefined }]).includes("<time"));
const odd = renderApprovalsHtml([{ ...base, details: { tier: 7, args: {} } }]);
check("unknown tier shows its number only", odd.includes("Tier 7<"));

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_APPROVAL_HTML_OK");
"""


@_NEEDS_RUNTIME
def test_approval_section_markup_under_real_js_runtime(tmp_path: Path) -> None:
    """Escaping, labels, tab order and in-flight state of renderApprovalsHtml()."""
    result = _run_js(tmp_path, _HTML_SCRIPT, _RUNTIME)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_APPROVAL_HTML_OK" in result.stdout


_STREAM_SCRIPT = r"""
globalThis.window = {
  location: { protocol: "http:", host: "127.0.0.1:1", search: "", pathname: "/", hash: "" },
  history: { replaceState() {} },
};
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; FakeWebSocket.last = this; }
  send() {}
  close() {}
}
globalThis.WebSocket = FakeWebSocket;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fetchCalls = [];
let inFlight = 0;
let maxInFlight = 0;
let latencyMs = 0;
const approval = { approval_id: "a1", action: "execute_exploit", state: "pending_approval", details: { tier: 3 } };
globalThis.fetch = async (url) => {
  fetchCalls.push(String(url));
  inFlight += 1;
  maxInFlight = Math.max(maxInFlight, inFlight);
  if (latencyMs) await sleep(latencyMs);
  inFlight -= 1;
  return {
    ok: true,
    json: async () => ({ task_id: "run-1", status: "completed", output: "o", error: null, artifacts: [], pending_approvals: [approval] }),
  };
};

const { createEventStream } = await import("./event-stream.js");
const { executionStore } = await import("./state.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

executionStore.setState({ executionId: "run-1", status: "running", task: null });
const stream = createEventStream();
stream.connect("run-1");
const ws = FakeWebSocket.last;
const send = (msg) => ws.onmessage({ data: JSON.stringify(msg) });
const approvalEvent = (seq, kind = "approval") => send({ type: "event", seq, event: { kind, payload: { approval_id: "a" + seq } } });

// A reconnect replays every event: a burst must cost one refetch.
for (let i = 1; i <= 30; i += 1) approvalEvent(i);
send({ type: "status", status: { status: "awaiting_approval" } });
send({ type: "status", status: { status: "paused" } });
check("nothing is fetched synchronously", fetchCalls.length === 0);
await sleep(600);
check("a burst is coalesced into one refetch", fetchCalls.length === 1);
check("the refetch asks for this run's detail", fetchCalls[0] === "/tasks/run-1");
check("pending approvals reach the store", executionStore.getState().task?.pending_approvals?.[0]?.approval_id === "a1");

// Lifecycle chatter is not an approval signal.
fetchCalls.length = 0;
send({ type: "event", seq: 100, event: { kind: "node_started", node: "n1" } });
send({ type: "status", status: { status: "running" } });
await sleep(400);
check("unrelated events do not refetch", fetchCalls.length === 0);

// Durable runs name the events differently.
approvalEvent(101, "approval_requested");
await sleep(400);
check("approval_requested refetches", fetchCalls.length === 1);
approvalEvent(102, "approval_decided");
await sleep(400);
check("approval_decided refetches", fetchCalls.length === 2);

// A signal during a slow fetch queues exactly one follow-up; never two in flight.
fetchCalls.length = 0;
maxInFlight = 0;
latencyMs = 400;
approvalEvent(200);
await sleep(300);
approvalEvent(201);
await sleep(1600);
check("never two requests in flight", maxInFlight === 1);
check("one follow-up refetch", fetchCalls.length === 2);

// Leaving the run cancels a pending refetch.
latencyMs = 0;
fetchCalls.length = 0;
approvalEvent(300);
stream.disconnect();
await sleep(500);
check("disconnect cancels a pending refetch", fetchCalls.length === 0);

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_APPROVAL_STREAM_OK");
process.exit(0);
"""


@_NEEDS_RUNTIME
def test_event_stream_refetches_pending_approvals_without_storms(tmp_path: Path) -> None:
    """Approval events / paused / awaiting_approval refetch GET /tasks/{id}, debounced."""
    result = _run_js(tmp_path, _STREAM_SCRIPT, _RUNTIME)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_APPROVAL_STREAM_OK" in result.stdout


_LINKEDOM_ROOT = _find_linkedom_root()

_DOM_SCRIPT = r"""
import { parseHTML } from "linkedom";

const { window, document } = parseHTML("<!doctype html><html><body><div id='pane'></div></body></html>");
globalThis.window = window;
globalThis.document = document;
globalThis.navigator = window.navigator;
globalThis.HTMLElement = window.HTMLElement;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jsonRes = (status, body) => ({ ok: status < 400, status, statusText: String(status), json: async () => body, text: async () => JSON.stringify(body) });
const textRes = (status, text) => ({ ok: false, status, statusText: String(status), json: async () => { throw new Error("not json"); }, text: async () => text });

const mk = (id, action) => ({ approval_id: id, task_id: "run-1", action, details: { tier: 3, args: { target: "10.0.0.1" } }, state: "pending_approval", requested_at: "2026-10-03T12:34:56.789000Z" });
let pending = [mk("ap-1", "execute_exploit")];
let mode = "ok";
let hold = Promise.resolve();
const calls = [];

globalThis.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  calls.push({ method, url: String(url), body: init.body });
  if (method === "POST" && /\/approvals\/[^/]+\/decide$/.test(String(url))) {
    await hold;
    if (mode === "fail") return textRes(500, "boom");
    if (mode === "conflict") {
      pending = [];
      return textRes(409, JSON.stringify({ detail: "approval ap-1 already decided (approved); decisions are terminal" }));
    }
    pending = pending.filter((a) => !String(url).includes(a.approval_id));
    return jsonRes(200, { state: "approved" });
  }
  if (String(url).startsWith("/tasks/")) {
    return jsonRes(200, { task_id: "run-1", status: "completed", output: "o", error: null, artifacts: [], pending_approvals: pending });
  }
  return jsonRes(200, []);
};

const { initInspector } = await import("./inspector.js");
const { executionStore, uiStore } = await import("./state.js");

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const pane = document.getElementById("pane");
const buttons = () => [...pane.querySelectorAll(".btn-approval-decide")];
const btn = (decision) => pane.querySelector(`.btn-approval-decide[data-decision="${decision}"]`);
const toasts = () => uiStore.getState().toasts;
const postCount = () => calls.filter((c) => c.method === "POST").length;

executionStore.setState({
  executionId: "run-1",
  status: "completed",
  task: { task_id: "run-1", status: "completed", pending_approvals: pending },
});
initInspector(pane);

// --- renders at the top of the pane --------------------------------------
check("section is first in the pane", pane.firstElementChild?.className.includes("approvals-section"));
check("one card per approval", pane.querySelectorAll(".approval-card").length === 1);
check("buttons are named after the action", btn("approve")?.getAttribute("aria-label") === "Approve execute_exploit" && btn("reject")?.getAttribute("aria-label") === "Reject execute_exploit");
check("nothing steals focus onto a button", !pane.innerHTML.includes("autofocus") && !buttons().includes(document.activeElement));

// --- approve: in flight, no double submit, then gone ---------------------
let release;
hold = new Promise((r) => { release = r; });
btn("approve").click();
btn("approve").click();
await sleep(20);
check("buttons disable while in flight", buttons().length === 2 && buttons().every((b) => b.hasAttribute("disabled")));
check("in-flight label", btn("approve").textContent.includes("Approving…"));
check("a double click sends one request", postCount() === 1);
check("request body", calls.find((c) => c.method === "POST").body === JSON.stringify({ approved: true, decided_by: "user" }));
release();
await sleep(60);
check("decision posted to the approval's endpoint", calls.some((c) => c.method === "POST" && c.url === "/approvals/ap-1/decide"));
check("task detail refetched after the decision", calls.some((c) => c.method === "GET" && c.url === "/tasks/run-1"));
check("section disappears", !pane.querySelector(".approvals-section"));
check("store no longer lists it", executionStore.getState().task.pending_approvals.length === 0);
check("approve toast says what was decided", toasts().some((t) => t.title === "Approval recorded" && t.message.includes("Approved execute_exploit") && t.message.includes("not restarted")));

// --- reject on a stale list, server says 409 -------------------------------
pending = [mk("ap-1", "execute_exploit")];
executionStore.setState({ task: { task_id: "run-1", status: "completed", pending_approvals: pending } });
check("section returns for a new pending approval", !!pane.querySelector(".approvals-section"));
mode = "conflict";
hold = Promise.resolve();
btn("reject").click();
await sleep(60);
check("409 says it was already decided", toasts().some((t) => t.kind === "warn" && t.title === "Already decided" && t.message.includes("already decided")));
check("409 refreshes the list", !pane.querySelector(".approvals-section"));

// --- generic failure: toast, buttons come back, approval stays -------------
pending = [mk("ap-2", "deploy_patch")];
executionStore.setState({ task: { task_id: "run-1", status: "completed", pending_approvals: pending } });
mode = "fail";
btn("reject").click();
await sleep(60);
check("failure toast carries the error", toasts().some((t) => t.kind === "error" && t.title === "Decision Failed" && t.message.includes("boom")));
check("buttons are usable again", buttons().length === 2 && buttons().every((b) => !b.hasAttribute("disabled")));
check("the approval is still listed", pane.querySelectorAll(".approval-card").length === 1);

// --- retry succeeds, reject message ---------------------------------------
mode = "ok";
btn("reject").click();
await sleep(60);
check("reject body", calls.filter((c) => c.method === "POST").pop().body === JSON.stringify({ approved: false, decided_by: "user" }));
check("reject toast", toasts().some((t) => t.title === "Rejection recorded" && t.message.includes("Rejected deploy_patch")));
check("section gone after reject", !pane.querySelector(".approvals-section"));

// --- stays on top of other inspector views --------------------------------
pending = [mk("ap-3", "rotate_keys")];
executionStore.setState({ task: { task_id: "run-1", status: "completed", pending_approvals: pending } });
uiStore.setState({ selectedResource: { type: "agent", data: { name: "llm", capabilities: [] } } });
check("approvals also sit above a resource view", pane.firstElementChild?.className.includes("approvals-section") && pane.innerHTML.includes("Agent Resource"));

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_APPROVAL_DOM_OK");
process.exit(0);
"""


@pytest.mark.skipif(
    BUN is None or _LINKEDOM_ROOT is None,
    reason="needs Bun + linkedom to run ui/js/inspector.js against a DOM",
)
def test_decide_flow_under_real_dom(tmp_path: Path) -> None:
    """Click Approve/Reject in the real inspector: in-flight state, toasts, refetch, errors."""
    env = dict(os.environ)
    env["NODE_PATH"] = str(_LINKEDOM_ROOT)
    result = _run_js(tmp_path, _DOM_SCRIPT, BUN, env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ALL_APPROVAL_DOM_OK" in result.stdout
