# UI/UX Crosscheck — Visual Canvas IDE

Date: 2026-10-02 · Method: two independent reviews, then cross-validated against the code.
- **UICrosscheck** (frontend agent) — code-grounded review of `ui/`
- **BunnyReview** — review by **space-bunny-alpha** (`openrouter/stealth/space-bunny-alpha` via 9router; `nr/space-bunny-alpha` was returning upstream errors at the time), with every model finding validated against the code by a second agent.

Verdict (space-bunny-alpha, harsh): *"This exposes execution machinery, not a usable task IDE — a user can start a run but cannot reliably see what happened or retrieve the answer."*

---

## The consensus P1 set (both reviewers, independently)

### 1. No result surface — the platform's output is invisible
`event-stream.js` writes `executionStore.output` from status messages, but **no UI code ever renders it** (`grep` of `inspector.js`/`index.html`: zero reads). After a run completes, the user sees a node graph and raw JSON events — never the actual answer (report text, artifacts, errors).
**Fix:** add an "Execution Output & Artifacts" section in the inspector (`inspector.js:69` area), with copy button; render `output` for completed/failed/clarification states.

### 2. Empty canvas is indistinguishable from broken
`graph-canvas.js:116-120` — when there's no graph it clears both layers and returns. What remains: an infinite dot grid with no message, no CTA, nothing to distinguish "no task yet" from "loading" from "disconnected".
**Fix:** empty-state overlay inside the viewport ("No Active Workflow" + "Enter a task and Run" CTA + example task button).

### 3. Silent failures on the primary action
- `index.html:258` — `handleRunTask`: empty input → `return` (no focus, no validation message, nothing).
- `index.html:267,270` — clarification/error surfaced via blocking `alert()`.
- `inspector.js` Pause/Resume/Fork → `send()` returns `false` silently when the socket is closed; no feedback, no offline banner; `connecting` status has no badge style.
**Fix:** inline validation + toast (the `uiStore.toasts: []` array already exists in `state.js:65` and is unused), disable runtime controls when disconnected.

### 4. False metrics shown as real
"Checkpoints: 0" and "Tokens: 0" are **hardcoded defaults** — the WS handler never populates `checkpoints[]`/`tokenUsage`; `cost` is never set. The UI reports zeros as if they were measurements.
**Fix:** show "not reported" until the data source exists (or wire the fields).

### 5. Keyboard/undo traps (BunnyReview, all CONFIRMED)
- Global `Ctrl+Z`/`Ctrl+Shift+Z`/`Ctrl+Y` handler calls `preventDefault()` **even while typing in the task input** — you cannot undo typed text.
- Undo stays **permanently disabled** after the auto-pushed graph load (`canUndo()` requires `pointer > 0`; `selectExecution` pushes exactly one snapshot).

### 6. Accessibility floor not met
- No `:focus-visible` rule anywhere (`app.css:370-388`) — keyboard navigation is invisible.
- Tabs are bare `<button>`s (no `role=tab`/`aria-selected`); canvas SVG nodes unreachable by keyboard/SR; task input has placeholder only, no label.
- `alert()`/`window.prompt()` (fork label) are blocking and unstyled.

---

## Additional confirmed findings (single-reviewer)

| Finding | Source | Evidence |
|---|---|---|
| Library items are inert — rendered with `data-ref` but zero click/drag listeners | UICrosscheck | `index.html:187-195` vs runs at `219-224` |
| Missing badge styles: `idle`, `connecting`, `pending`, `skipped` fall back to unstyled text | both | `app.css:433-437` defines only running/completed/failed/paused |
| Zoom buttons drift toward (0,0) — button zoom ignores viewport center (wheel zoom handles it) | UICrosscheck | `graph-canvas.js:324-333` vs `307-308` |
| `resetView` goes to 100%/0,0 — a loaded graph can render offscreen; no fit-to-content | BunnyReview | controller exposes only zoomIn/Out/resetView |
| `selectExecution` race — rapid run switching can let a stale fetch overwrite the newer graph | BunnyReview | no AbortController/request-id |
| Layout breaks below ~960px — fixed 280px/320px panes, `overflow:hidden`, no media queries | BunnyReview | `app.css:92,188` |
| Vocabulary drift: Task / Run / Workflow / Execution used interchangeably | both | input vs tabs vs inspector vs API |
| Approval flow has backend + API (`decideApproval`) but **no UI at all** when execution pauses | both | `api.js:50` unused by any view |

## space-bunny-alpha's hallucinations (validated WRONG — worth noting)

4 of its 20 findings were invented: port dots "lack labels" (they render `<text>` names), header "contradiction" (label updates correctly), "duplicate WebSocket leaks" (disconnect() cleans up), "wheel zoom hijacks page scroll" (preventDefault + `overflow:hidden` body). Also: it returned empty `content` with `finish_reason=length` — the critique had to be recovered from its reasoning channel. **Use it for breadth, verify everything.**

---

## What's actually fine

- Pane proportions are functional; node port labels exist; SSE reconnect logic is present; badge classes for the *implemented* statuses are correct; the canvas dot grid renders cheaply.

## Suggested fix order (if executed)

```
P1  result surface (inspector output panel)        <- the one that changes the product
P1  canvas empty state + CTA
P1  silent failures -> inline validation + toasts + offline banner
P2  false metrics -> "not reported"
P2  keyboard traps (guard Ctrl+Z in inputs; fix undo pointer)
P2  focus-visible + ARIA pass (tabs, input label, buttons)
P2  missing badge styles + error-box
P3  library click/drag, edge interaction, fit-to-view, resize handles, responsive layout
```
