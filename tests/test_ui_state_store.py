"""Behavioral regression tests for the Canvas UI state store.

The bug these lock in (found 2026-10-02 via browser automation):
``createStore().setState(fn)`` REPLACED the whole state instead of merging, but
every call site passes a partial patch. Two user-visible failures followed:

1. A lifecycle event's ``setState((s) => ({activeNodes: ...}))`` wiped
   ``executionStore.status`` -> the badge showed ``undefined`` forever.
2. A canvas pan's ``setState((s) => ({viewport: ...}))`` wiped
   ``canvasStore.graph`` -> the pipeline vanished after any click on the canvas.

These tests run the REAL ``ui/js/state.js`` under Bun (a JS runtime is required;
the suite skips cleanly when Bun/node is unavailable).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"

BUN = shutil.which("bun")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    BUN is None and NODE is None,
    reason="needs a JS runtime (bun or node) to execute ui/js/state.js",
)

_RUNTIME = BUN or NODE

# One script, run by the real JS runtime, asserting the store contract.
_SCRIPT = r"""
import { createStore } from "./state.js";

const failures = [];
function check(name, cond) {
  if (!cond) failures.push(name);
}

// --- Contract 1: functional updater MERGES, never replaces -------------
const store = createStore({ a: 1, b: 2, c: 3 });
store.setState((s) => ({ b: 99 }));
check("merge keeps a", store.getState().a === 1);
check("merge keeps c", store.getState().c === 3);
check("merge applies b", store.getState().b === 99);

// --- Contract 2: object patch MERGES too ------------------------------
store.setState({ c: 42 });
check("object patch keeps a", store.getState().a === 1);
check("object patch applies c", store.getState().c === 42);

// --- Contract 3: THE REAL REGRESSION ----------------------------------
// executionStore: a node-status patch must not wipe `status`
const exec = createStore({ status: "running", executionId: "x", activeNodes: {} });
exec.setState((s) => ({ activeNodes: { ...s.activeNodes, n1: "completed" } }));
check("node patch keeps status", exec.getState().status === "running");
check("node patch keeps executionId", exec.getState().executionId === "x");
check("node patch applies node", exec.getState().activeNodes.n1 === "completed");

// canvasStore: a viewport patch must not wipe `graph`
const canvas = createStore({ graph: { nodes: [1, 2, 3] }, viewport: { x: 0, y: 0, zoom: 1 } });
canvas.setState((s) => ({ viewport: { ...s.viewport, x: 50 } }));
check("viewport patch keeps graph", canvas.getState().graph?.nodes?.length === 3);
check("viewport patch applies x", canvas.getState().viewport.x === 50);

// --- Contract 4: listeners fire with the merged state ------------------
let seen = null;
const sub = createStore({ keep: "yes", change: "no" });
sub.subscribe((s) => { seen = s; });
sub.setState({ change: "yes" });
check("listener got merged state", seen && seen.keep === "yes" && seen.change === "yes");

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_STORE_CONTRACTS_OK");
"""


def test_store_semantics_under_real_js_runtime(tmp_path: Path) -> None:
    """Run ui/js/state.js and assert merge semantics + the two regressions."""
    script = tmp_path / "check_state.mjs"
    # state.js is a plain ES module with no imports; copy it next to the script
    # so the relative import resolves.
    shutil.copy(UI_JS / "state.js", tmp_path / "state.js")
    script.write_text(_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [_RUNTIME, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, (
        f"store contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ALL_STORE_CONTRACTS_OK" in result.stdout


def test_ui_source_uses_merge_style_updaters() -> None:
    """Guard the call-site style the store now guarantees (merge patches)."""
    state_src = (UI_JS / "state.js").read_text(encoding="utf-8")
    assert "const nextState = { ...state, ...patch };" in state_src

    event_src = (UI_JS / "event-stream.js").read_text(encoding="utf-8")
    # The lifecycle handler must return only the patch (merge is the store's job)
    assert "executionStore.setState((s) => ({" in event_src

_UNDO_SCRIPT = r"""
import { createUndoStack } from "./undo.js";

const failures = [];
function check(name, cond) {
  if (!cond) failures.push(name);
}

// Regression 2026-10-02: canUndo() required pointer > 0, so the auto-pushed graph
// load (pointer = 0) left undo permanently disabled even after user edits.
const undo = createUndoStack({ limit: 100 });
check("empty stack canUndo is false", undo.canUndo() === false);
check("empty stack canRedo is false", undo.canRedo() === false);

// 1. Auto-pushed graph load (pointer becomes 0)
undo.push({ nodes: ["initial-load"] }, { actor: "ai", description: "Loaded run", previous: null });
check("after auto-push canUndo is true", undo.canUndo() === true);
check("after auto-push canRedo is false", undo.canRedo() === false);

// 2. First user change (pointer becomes 1)
undo.push({ nodes: ["initial-load", "user-node"] }, { actor: "user", description: "Node added" });
check("after user change canUndo is true", undo.canUndo() === true);

// 3. Undo user change -> returns initial graph
const rolledBack = undo.undo();
check("undo rollbacks to initial-load", JSON.stringify(rolledBack) === JSON.stringify({ nodes: ["initial-load"] }));
check("canRedo is true after undo", undo.canRedo() === true);

// 4. Undo auto-push -> rolls back to pre-load baseline (null)
const baseline = undo.undo();
check("undo to baseline returns null", baseline === null);
check("canUndo at baseline is false", undo.canUndo() === false);
check("canRedo after baseline undo is true", undo.canRedo() === true);

// 5. Redo restores initial-load
const restored = undo.redo();
check("redo restores initial-load", JSON.stringify(restored) === JSON.stringify({ nodes: ["initial-load"] }));
check("canUndo after redo is true", undo.canUndo() === true);

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_UNDO_CONTRACTS_OK");
"""


def test_undo_stack_semantics_under_real_js_runtime(tmp_path: Path) -> None:
    """Verify undo stack permits undoing from pointer 0 to baseline (regression test)."""
    script_file = tmp_path / "check_undo.mjs"
    script_file.write_text(_UNDO_SCRIPT, encoding="utf-8")

    (tmp_path / "undo.js").symlink_to(UI_JS / "undo.js")

    result = subprocess.run(
        [_RUNTIME, str(script_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, f"script failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "ALL_UNDO_CONTRACTS_OK" in result.stdout

