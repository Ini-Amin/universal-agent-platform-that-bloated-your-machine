# Handover — what happened while you slept

Date: 2026-10-03 · Status: **all work committed and pushed** (`3d78b6b`)

---

## TL;DR

I drove the real UI in a browser (headed, so you could watch), found 6 real bugs,
fixed all of them, verified each one independently, and pushed.

**Suite: 1113 passed, 5 skipped** (was 1083 before — 30 new tests).
**Three commits:** `b1382b0` (fixes) → `3d78b6b` (docs).

---

## What I found (all reproduced, not guessed)

### 1. Mobile was unusable — 144px of horizontal overflow
Measured with `getBoundingClientRect`: `.toolbar-group` was 421px and
`.header-status` 228px wide inside a 390px viewport. Both were plain flex rows
with no wrap. Separately, artifact paths rendered **one character per line** —
`.artifact-uri` was a flex item with `word-break: break-all`, so its min-content
width was a single glyph and flex crushed it.

**Now:** `scrollWidth == clientWidth` at 390px, panes stack vertically, paths wrap
at sensible points. Verified in a **clean browser profile** (a cached CSS file had
been hiding the fix).

### 2. The Events panel showed 2 of 23 events — root cause was server-side
This was the most interesting one. The WS layer asked for events using the id the
UI knows (the one `POST /tasks` returns), but the server stores that value as
`correlation_id` — the row has its own primary key. `read_since` matched **zero
rows**, so the bridge silently fell back to the in-memory sink, which only holds
the two coarse lifecycle events. `status` and `checkpoints` had the same gap.

**Now:** all three resolve either id form. Panel renders 23 distinct rows.

### 3. Duplicate tasks in `GET /tasks`
One RunRecord was registered under two keys (task_id AND execution_id) and the
listing iterated `values()` without dedup. The **database was never duplicated**
(2019 executions = 2019 distinct correlation ids, zero orphans) — it was purely
an API-layer artifact.

### 4. Sidebar said "running" while the header said "completed"
The run list is fetched from `GET /tasks` and was only re-fetched when a run
*started*. On completion the badge updated but the sidebar went stale.

### 5. Three security holes (all Low, all reproduced)
- `/openapi.json`, `/docs`, `/redoc` returned **200 without a token** while
  `/tasks` correctly returned 401 — FastAPI registers these on the Starlette
  router, so the app-level dependency never ran for them.
- `GET /api/resources/mcp` echoed an absolute host path (`/home/user/bugbounty-mcp/bbmcp`).
- `ArtifactStore.save()` skipped the root-containment guard the other methods
  apply — a symlinked task dir could write outside the store root.

### 6. A browser could keep serving the PREVIOUS build
The UI is mounted as static files with relative URLs. A cached `app.css` made a
**verified** layout fix look broken during review. UI assets now send
`Cache-Control: no-cache, must-revalidate`; API responses are untouched.

---

## Licensing (this was task "A")

- **AGPL-3.0** installed, `pyproject.toml` updated.
- The UI now carries a **"Source" link** — AGPL §13 requires that network users
  be offered a way to obtain the source. A regression test enforces it.

Why AGPL: it stops a competitor from taking the code, changing a little, and
selling it as a hosted service without contributing back. If you later want
open-core, add an `enterprise/` directory under a different license.

---

## Honest corrections (worth reading)

**I was wrong about the traces endpoint.** I reported it leaked another run's
data. An independent agent disproved it: the two ids were the `correlation_id`
and the row id of the **same** run. I retracted the instruction before any code
changed. This is recorded in the business doc so the mistake is not repeated.

**A regression test initially passed vacuously.** Its input routed to a
clarification, so the listing was empty and the uniqueness assertion held
trivially — it proved nothing. It now uses a routing input and asserts the list
is non-empty.

Both are worth knowing because they are the failure modes that make an
orchestrator dangerous: confidently "fixing" working code, and shipping a test
that cannot fail.

---

## The 9 agents

I created 9 project-specific agents, each pinned to **1M-context models from
your `mocin` combo** (verified present in the combo before use):

| Agent | Purpose | Model (primary) |
|---|---|---|
| `uap-ui` | layout, responsive | glm-5.3-flash |
| `uap-css` | styling, rendering | GLM-5.3-Flash |
| `uap-api` | FastAPI routes, data flow | deepseek-v4.1-flash |
| `uap-data` | SQL, consistency | deepseek-v4.1-flash |
| `uap-tests` | regression tests | deepseek-v4.1-flash |
| `uap-docs` | user docs | GLM-5.3-Flash |
| `uap-audit` | adversarial verification | kimi-k3 |
| `uap-perf` | latency, streams | step-5-preview |
| `uap-sec` | API security review | space-bunny-alpha |

**Two model lessons:** `cx/gpt-6-sol` fails with "not supported when using Codex
with a ChatGPT account", and `cbai/deepseek-v4-pro` returns "model service info
not found". Both were removed after agents failed on them. Also, the free quota
on `kimi-k3` / `glm-5.3-flash` ran out mid-session, so I finished the last items
myself.

---

## Verification I ran against my own work

I tried to disprove every claim:

| Attack | Result |
|---|---|
| Dedup with 3 runs (not just 1) | PASS — 3 total, 3 unique |
| AGPL link in live HTML (not a comment) | PASS — real anchor |
| `/docs` without token (auth on) | 401 ✅, with token 200 ✅ |
| Auth OFF — behaves as before | All 200, unchanged |
| Symlink escape via `ArtifactStore.save()` | BLOCKED, 0 files outside |
| Cache middleware breaking API/SSE | API clean, SSE streams fine |
| Full user journey in browser | 23 events, 0 dupes, statuses agree, 0 overflow |

---

## Open items (not done)

1. **Unbounded growth.** `execution_checkpoints` is 38 MB for 2k runs (~10
   full-state snapshots per run, each growing with node depth) and **nothing is
   ever pruned** — not executions, events, traces, or checkpoints. At the
   observed rate (677 runs/hour) this is the most likely disk-full incident.
2. **No multi-user isolation.** One token for everyone; there is no per-user
   data boundary. Required before selling anything.
3. **Only 2 of 6 domains are implemented** (`research`, `bbp`). `learning`,
   `coding`, `data` are routed but have no workflow.
4. **Finding 2** (host path leak) is fixed and committed — no action needed.

---

## Files to read

- `docs/architecture/10-business-flow-discussion.md` — the business/UX analysis
  you asked for: audience decision, what the product is today, and the fork in
  the road (developer tool vs beginner tool).
- `README.md` — new section: "What UAP can do right now (read this first)".
  Every example in it was run against a live server before being written down.
