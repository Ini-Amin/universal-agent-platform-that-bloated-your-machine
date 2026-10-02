# UAP Implementation Matrix — Audit vs Master Spec

Date: 2026-10-02 · Method: 3 read-only scouts tracing **actual call chains** (not file existence), plus orchestrator spot-verification.
Status vocabulary (spec §0): 1=implemented+integrated · 2=implemented-not-integrated · 3=stub · 4=test-only · 5=partial · 6=inconsistent · 7=missing

---

## A. Execution path (AuditA, verified)

| Area | Status | Evidence | Problem | Action |
|---|---|---|---|---|
| Entry → Router → Workflow | 1 | `app.py:540→546→553` | works end-to-end | — |
| Research workflow nodes | 5 | `research.py:165-327` | nodes 1,2,4,5,6,8 real; **node 3 (fan_out) calls stub collectors** | real collectors or fail-closed |
| Research evidence | **3** | `research.py:56-74` | **fake URLs** (`example.com/python`, `arxiv.org/abs/0001`) rendered under **"## Verified findings"** | kill or label |
| BBP workflow | 5 | `bbp.py:292-483` | scope gate IS enforced (`app.py:462-500`, `bbp.py:292-332`) | — |
| BBP recon | **3** | `bbp.py:122-192` | fabricates subdomains, endpoints, **4 vulnerabilities** that pass validation into `report.md`/`findings.json` | wire real bbmcp or label |
| RunRecord persistence | 6 | `app.py:93-120, 511-536` | keeps status/output/artifacts; **discards** WorkflowState.data, VerificationResult, user_id, checkpoints | persist to PG |
| §71 PlatformSlice | **4** | `slice/orchestrator.py:159` — imported ONLY by tests | server bypasses the entire §71 spine (workspace, library, durable engine, knowledge, evaluator) | make it the primary path |
| ExecutionService (PG) | **2** | `app.py:403` instantiated with **dummy resolver raising KeyError**; only WS reads it | never enqueues; no Worker started | route /tasks through it |
| Server restart durability | **6** | `app.py:329` in-memory `runs` dict | all runs lost on restart; artifacts + events.jsonl orphaned | hydrate from PG |
| §49 disclosure | **7** | zero disclaimer in report templates | fake findings presented as "Verified" with severity badges | SIMULATION labels |

**Fake data inventory (user-visible):** 4 research claims w/ fake URLs · 2 fake BBP assets · 4 fake BBP findings (subdomain takeover, missing header, CORS, XSS) — all reach `report.md`, `sources.json`, `findings.json`, `GET /tasks/{id}`.

---

## B. Runtime / durability (AuditB, verified)

| Subsystem | Status | Evidence | Action |
|---|---|---|---|
| `runtime/` ExecutionService + Worker | **2** | called only by slice + tests | wire to /tasks |
| `execution/` GraphExecutor | **2** | called only by `ExecutionService._get_executor` (`service.py:208`) | unify workflows under it |
| `scheduler/`, `recovery/`, `faults/`, `replay/` | **4** | imports only in their own tests; zero src callers | integrate into Worker |
| `coordination/`, `dynamic/`, `subworkflows/`, `trust/` | **4** | zero callers in src outside own packages | wire or explicitly defer |
| Checkpoints (UI runs) | **6** | `runner.py:145,272` use InMemoryStateStore; PG checkpoints only via ExecutionService | persistent StateStore |
| Browser reconnect | **5** | `ws_bridge.py:52` replays events; `node_history` empty until finish; pause fails | hydrate from checkpoint store |
| Events | **5** | `events.jsonl` written but never read; no PG sink; ReplayEngine can't replay /tasks runs | PG EventRepository sink |
| Restart survival | **6** | runs dict + memory_sink in RAM | persist + hydrate |

---

## C. Resources / governance (AuditC, verified)

### GOVERNANCE ENFORCEMENT MAP — the verdict

| Guard | Enforced at | Status |
|---|---|---|
| `policy` PolicyEngine | **nowhere** (only `tools.policy` tier checks) | NOT ENFORCED |
| `capability` CapabilityResolver | **nowhere** (grants/HMAC tokens never checked) | NOT ENFORCED |
| `credentials` CredentialManager | **nowhere** (no secret resolution at call sites) | NOT ENFORCED |
| `sandbox` Sandbox | **nowhere** (tools run in host process) | NOT ENFORCED |

| Subsystem | Status | Evidence | Action |
|---|---|---|---|
| ContextCompiler | **2** | zero callers outside tests; slice builds AgentContext manually with 1 section | wire into PlatformNodeRuntime + LLMAgent |
| Memory (SQLite) | **2/4** | zero runtime callers; §26 conflict if used | migrate to PG or deprecate |
| Skills | **2** | test-only registration | bind into entry/router/workflow prompts |
| Knowledge | **2** | slice-only | post-run extraction in server |
| Policy/capability/credentials/sandbox | **2/7** | zero src callers outside own packages | guards in ToolRegistry.call |
| ApprovalGate | **2** | instantiated `app.py:327`, exposed via endpoint; **zero workflow usage** | suspend on tier-3 tools |
| Workspace/Library | **2** | zero occurrences in server; workflows hardcoded `app.py:320-324` | detect + resolve from library |
| Evaluation/learning/packages/gitx | **2** | zero calls in server/runner | post-run hooks |
| ModelRouter | **2** | zero production callers; LLMAgent falls back to hardcoded model string | instantiate + inject |
| MCP | **6/5** | started and passed to BBPWorkflow, but recon step names (`stub_subdomain_recon`) ≠ MCP tool names (`find_subdomains_passive`) → **every MCP call skipped** | align names; call real tools |

---

## Contradictions against the spec (ranked)

1. **§49 violation (fake data as real):** stub collectors present fabricated findings as "Verified" — the single most serious product-integrity issue. Fix: label + replace.
2. **§50 violation (fake complexity):** ~10 subsystems (scheduler, recovery, faults, replay, coordination, dynamic, subworkflows, trust, capability, sandbox) are test-only with zero production callers — architecture exists, product doesn't use it.
3. **§17/§48 violation (durability):** browser disconnect/reconnect and server restart lose execution state; the durable runtime exists but is bypassed.
4. **§51 violation (frontend/backend split):** server path never produces the canonical graph the UI needs — hence the (now-deleted) placeholder graph.
5. **§2/§58 violation (product identity):** `/tasks` is a linear WorkflowRunner with no workspace, no proposal, no policy, no context compile — while the §71 slice (which has all of that) is test-only.

---

## The strategic fix (spec §57: integration over new architecture)

**Make the §71 slice the primary `/tasks` path.** It already contains: workspace detection, library versioning, canonical graph execution (PG-backed), event store, decision traces, knowledge lifecycle, artifacts, slice evaluator, git definitions. The server path has none of it.

Order (each step keeps the suite green):
```
1. Label stub evidence everywhere (§49)                      — immediate integrity fix
2. Route /tasks → PlatformSlice (durable spine)              — the big unification
3. GraphExecutor as the single execution engine              — unify runner + slice
4. PG event sink for ALL runs (kill events.jsonl-only)       — replay + restart survival
5. Policy/capability enforcement at ToolRegistry.call        — governance on the real path
6. Wire scheduler/recovery into Worker; trust/coordination   — only after 1-5 are real
```

Steps 5–6 are deliberately last: per §57, fixing fake/misleading behavior and integrating existing subsystems outranks adding new wiring.

### Additional findings from AuditC (added)

- **MCP is effectively dead in BBP:** tools are started and passed in, but `BBPWorkflow` recon step names (`stub_subdomain_recon`) don't match MCP tool names (`find_subdomains_passive`) → every `ToolRegistry.call` raises unknown-tool and is skipped. Fix by aligning names (spec §21).
- **ContextCompiler never runs:** the slice builds `AgentContext` manually with a single input section; no retrieval/ranking/budgeting (spec §9). Wire into `PlatformNodeRuntime` + `LLMAgent`.
- **ModelRouter never runs in production:** `LLMAgent` defaults `router=None` → hardcoded `_DEFAULT_MODEL` (spec §25). Instantiate at server boot and inject.
- **ApprovalGate never gates anything:** instantiated in `app.py:327`, endpoint exists, zero workflow usage (spec §23).
- **Skills never load:** `SkillRegistry` is test-only (spec §8).
- **Memory (SQLite) never runs and would violate §26 if it did:** migrate to PG or deprecate.
- **Evaluation/learning/packages/gitx never run in `/tasks`:** only the slice uses gitx.

---

## Update 2026-10-02 (post-execution)

### Closed since the audit

| Gap | Status | Evidence |
|---|---|---|
| §49 fake-data disclosure | **FIXED** | research + BBP reports and findings.json carry SIMULATION banners; run records expose `evidence_source: deterministic-stubs` (verified live) |
| Fake placeholder graph | **DELETED** | `grep "Agent Core" src/uap/server/app.py` → 0; real graphs from `graph_spec()` |
| Real graph resolver | **FIXED** | `_default_graph_resolver` maps workflow names → real `WorkflowGraph` (was a KeyError stub) |
| Proposal before execution | **SHIPPED** | `POST /api/proposals` + UI proposal flow (10 real nodes previewed) |
| Resource inspection | **SHIPPED** | `/api/resources/{agents,tools,skills,models,policies,mcp}` + contextual inspector |
| Live execution state on canvas | **SHIPPED** | per-node status rings driven by real events (verified in browser) |
| Durable stack reachable | **WIRED** | `PlatformSlice` + `ExecutionService` initialize against PG; slice accepts injected task_id |

### The remaining critical integration (honest)

**The slice's research graph is a DEMO graph.** Verified: a slice run produces
`execution_events` for `input/recon/fetch/summarize/synthesize/evaluate/knowledge/output`,
and its `slice-synthesize` artifact is `{"probe": "ok"}` (15 bytes) — echo agents,
not research. Meanwhile the legacy `ResearchWorkflow` is the real 8-node pipeline.

Consequence: routing `/tasks` through the slice would **downgrade** the product
from real (stub-evidence-labelled) research to an echo probe. The product path
therefore deliberately stays on the legacy runner; the durable infrastructure
is built and waiting for the graphs to be unified.

**Next integration step (the one that matters):** make the slice execute the
REAL research graph — i.e. port the 8 research nodes into the canonical graph
nodes the slice's runtime adapter dispatches (agent nodes running the real
analysis/synthesis logic, tool nodes running the collectors), so durability,
traces, and knowledge apply to the real pipeline instead of the demo.

That is one focused piece of work: `PlatformNodeRuntime` needs agent handlers
that call the research pipeline's real node functions, and `build_research_graph`
needs to declare that pipeline's 8 nodes instead of the demo chain.

---

## Update 2026-10-02 (graph unification landed)

### The critical integration is DONE

**The durable slice now executes the REAL research pipeline.** `build_research_graph()`
declares the actual 8 pipeline nodes; `PlatformNodeRuntime` runs the real
`ResearchWorkflow` node functions through them (threading `WorkflowState.data`).
Verified output-equivalent to the legacy runner — same report, same SIMULATION
banner — plus full durability.

### What is now real on the product path

| Capability | Evidence |
|---|---|
| `/tasks` research → durable slice | `executions` row keyed by `correlation_id` = client task id |
| Durable event store | 19+ `execution_events` per run, node lifecycle |
| Live node statuses on the canvas | 10 rings show `completed`, read from PG events |
| Decision traces | `GET /api/executions/{id}/traces` → 1+ real trace |
| Restart durability | fresh server process: 10 nodes + statuses + traces recovered from PG |
| Knowledge lifecycle | verified claims from `cross_verification` proposed/verified/promoted |
| Artifacts | 9 per-node artifacts + `.meta.json` sidecars |
| §49 disclosure | SIMULATION banner in reports and findings.json |

### Bugs found and fixed during unification (all were silent)

1. `Execution` has **no `workflow_version` relationship** — the graph endpoint read a
   nonexistent attribute inside a bare `except`, so every durable lookup silently
   returned the fallback/404.
2. The version row's `spec` is a **library entry** (`{inputs, outputs, graph_ref}`),
   not a graph — `from_dict` always failed. Resolution now follows the ref to the
   workflow's `graph_spec()`.
3. Durable executions are keyed by `id`, while clients hold `correlation_id` —
   added `get_by_correlation_id` and made every lookup resolve **either** id.
4. The graph endpoint returned early on the durable branch, **skipping the status
   merge** — nodes always showed `pending`.
5. `_node_statuses` only read the in-memory sink; the slice emits to PG — now
   merges both sources.
6. The UI never re-fetched the graph after completion — rings stayed `pending`.
   Now re-fetches on terminal state.
7. Client `workspace_id` was ignored (slice always used detection) — explicit
   choice now wins.

### Still not unified (honest)

- **BBP** stays on the legacy path: its per-request scope gate is a security
  boundary the slice does not honour yet.
- Scheduler/recovery/faults/replay/coordination/dynamic/subworkflows/trust remain
  test-only.
- Governance (policy/capability/credentials/sandbox) still not enforced on the
  tool-call path.
- ContextCompiler / ModelRouter / ApprovalGate / Skills still off the product path.
