# Business Flow & Product Discussion

Date: 2026-10-03 · Status: draft for discussion (written while user asleep)
Method: live browser review + code reading + independent agent verification. Every claim below was reproduced, not assumed.

---

## 1. What was actually tested

A real Chromium session against the running app (port 8090, current code). Every finding has a reproduction.

**Important methodological note:** an earlier session tested port 8000, which was running 8-hour-old code. That produced a false "Generate proposal returns 405" finding. The server was stale, not the code. **Lesson: always verify which build is serving before reporting a bug.**

---

## 2. Confirmed findings (with evidence)

### 2.1 Mobile is unusable

| Measurement | Value |
|---|---|
| Viewport | 390 x 844 (iPhone 12/13/14) |
| `documentElement.scrollWidth` | 534 |
| `documentElement.clientWidth` | 390 |
| Horizontal overflow | **144 px** |

Two visible defects:
- **Long paths render one character per line.** Artifact paths (`data/runs/artifacts/<uuid>/report.md`) rendered vertically, one glyph per row. Cause: a flex/grid child without `min-width: 0`, so the container shrinks to the widest single character.
- **Event list duplicates each row 3x.** The panel showed the same event three times.

**Why it matters:** a first-time user on a phone sees a broken page. There is no desktop-only warning.

### 2.2 `GET /tasks` returns duplicate entries — root cause identified

Independently verified by a data agent:

```
DB:    SELECT id, correlation_id FROM executions WHERE correlation_id='7d0a5f01...'
       -> exactly 1 row (id=4ae377d8...)
API:   GET /tasks -> same task_id appears TWICE
```

**Root cause:** the in-memory `runs` registry stores one `RunRecord` object under **two keys** — the client-facing `task_id` and the durable `execution_id`. `GET /tasks` iterates `values()` with no deduplication.

**Scope check (important):** this is NOT data corruption. The database is clean:
- 2019 executions = 2019 distinct correlation_ids = 0 duplicates
- 0 orphaned events, 0 orphaned traces, 0 orphaned checkpoints

The duplication is purely a display/API-layer artifact. But any consumer that *counts* `/tasks` (a dashboard, a billing meter) would overcount by roughly the number of slice-path runs.

### 2.3 The traces endpoint is CORRECT (a false alarm, corrected)

I initially reported that `/api/executions/{id}/traces` returned another execution's data. **That was wrong**, and an independent agent disproved it:

- `7d0a5f01...` is the **correlation_id** (client-facing task id)
- `4ae377d8...` is the **row id** of the *same* logical run
- `TraceStore.list_for_execution` does filter by `execution_id`
- Requesting by either id returns the same, correctly-scoped set

I had misread two identifiers of one run as two different runs. The fix instruction was retracted before any code was changed.

**Why this is worth recording:** an orchestrator that cannot be corrected by its own verifiers will confidently break working code. The correction loop is the point.

### 2.4 Unbounded growth in two tables

| Table | Rows | Size | Per run |
|---|---|---|---|
| `execution_checkpoints` | 19,998 | **38 MB** | ~10 full-state JSONB snapshots |
| `execution_events` | 46,053 | 10 MB | ~23 events |
| `executions` | 2,019 | 2.7 MB | 1 |
| `decision_traces` | 3,153 | 1.5 MB | ~1.5 |

- **No retention or pruning code exists** for executions, events, traces, or checkpoints. Only the in-memory registry is capped (500).
- Checkpoint payload **grows with node depth**: avg per-seq rises 944 → 2,303 bytes.
- All 2,019 executions were created in **7.35 hours** — peak 677/hour.

**Projection:** at the observed rate, checkpoints alone add ~5 MB/hour of full-state duplication. This is the single most likely cause of a production disk-full incident.

### 2.5 Security findings (three, all reproduced)

| # | Finding | Severity | Evidence |
|---|---|---|---|
| 1 | `/openapi.json`, `/docs`, `/redoc` return **200 without a token** when auth is enabled, while `/tasks` correctly returns 401 | Low | FastAPI registers these on the Starlette router, so the app-level dependency never runs; `is_exempt()` doesn't cover them. Discloses route names + methods (incl. the governance route) to an anonymous visitor. |
| 2 | `GET /api/resources/mcp` echoes an absolute host path (`/home/user/bugbounty-mcp/bbmcp`) | Low | `src/uap/mcp/config.py:74` default is returned verbatim by `app.py:1640`. Discloses host layout. |
| 3 | `ArtifactStore.save()` omits the root-containment check that `get()`/`list()`/`delete_task()` apply | Low | A crafted id/type could escape the store root. |

**Honest severity note:** all three are Low. None discloses a secret, a credential, or another user's data. They matter because they are cheap to fix and because #1 contradicts the auth module's own documented contract.

### 2.6 No AGPL §13 source link

The LICENSE is now AGPL-3.0, but the UI has no link to the source. AGPL §13 requires that users interacting with the software over a network be offered a way to get the source. This is a **compliance gap**, not a style issue.

---

## 3. UX: what a first-time user actually experiences

Reproduced in the browser, step by step:

1. Page loads. Canvas is empty. Text says: *"Enter a task description above and click Generate proposal to preview the workflow graph, or Run to execute."* — two buttons, no explanation of the difference.
2. Placeholder: *"Enter task (e.g. research best checkpointers)..."* — developer jargon.
3. A natural first attempt — `"belajar python dari nol"` — returns:
   > *"The 'learning' domain is not available yet. Try a research request ("research ...") or a bug bounty request ("bug bounty on example.com")."*
4. Only **two** domains are executable: `research` and `bbp`. The Domain enum defines six (`learning`, `research`, `coding`, `bbp`, `data`, `unknown`); four are routed but not implemented.

**The honest position:** UAP today is a *developer/researcher tool* for two workflows. It is not a general assistant. Presenting it as one creates the exact failure in step 3.

---

## 4. The decision this implies

The UI is currently built for someone who wants to inspect execution machinery — graphs, traces, node lifecycle. That is a legitimate product, and it is ~80% right for its audience.

It is the wrong product for a non-technical user, who wants to type a request and receive a result.

**These are different products sharing a backend.** The fork in the road:

| Path | Who it is for | What changes | Effort |
|---|---|---|---|
| **A. Sharpen the developer tool** | Engineers, researchers, security teams | Keep the canvas. Fix mobile. Document the two supported domains. Make the graph the hero. | Small — mostly polish |
| **B. Add a beginner mode** | General users | New simple view: one input, one button, a result card. Canvas becomes an advanced tab. | Large — new surface |
| **C. Ship both** | — | B as the default entry, A behind a toggle | Largest |

**Recommendation: A first.** Reasons:
1. The audience that values graph + traces + governance is the audience that *pays* (engineering teams, security teams). A general assistant competes with ChatGPT on ground where UAP has no advantage.
2. A is small, so it can ship now and generate real usage signal.
3. B can be added later without rework — the backend is already domain-agnostic. Building B first risks polishing a surface no one has asked to pay for.

**What must be true before selling anything:**
- Multi-user isolation (today: one token for everyone — no per-user boundary)
- Retention/pruning for the unbounded tables (§2.4)
- A landing page that states honestly what the tool does and does not do

---

## 5. Open questions for the user

1. **Audience:** developer tool (A), beginner tool (B), or both (C)?
2. **Business model:** open-core (free core + paid enterprise), hosted SaaS, or consulting?
3. **Scope of "business":** a real company (needs legal entity, billing, support), or a portfolio/consulting asset?
4. **Deployment:** the DCloud trial (3 months free) — is the goal production, or a demo?
