# Multi-Agent Discussion: The 5 Next Steps (Architect × Researcher × Challenger)

Date: 2026-10-01 · Method: three independent agents — ARCHITECT (file-level plans), RESEARCHER (environment ground truth), CHALLENGER (adversarial review) — plus orchestrator verification of the decisive claims. No agreement theater.

---

## The verdict table (what survives)

| # | Step | Architect said | Challenger said | **Final verdict** |
|---|---|---|---|---|
| 1 | Wire LLMAgent | S effort, ~120 lines | **NEEDS MAJOR REWORK** — graph hardcodes `agent: "echo"`; registering LLMAgent changes NOTHING | **DO IT — but scope it right (M/L, graph redesign)** |
| 2 | Wire MCP server | S, ~25 lines | **SURVIVES** — framing verified compatible; 20s timeout exists; honest degradation | **DO IT — unchanged, add to_thread note** |
| 3 | CI workflow | S, one YAML | **NEEDS CHANGES** — 3.15 rc needs `allow-prereleases`; pyproject missing most deps | **DO IT — after fixing pyproject deps** |
| 4 | Containerfile | S, 2 files | **DROP** — no `python:3.15-slim` exists; podman compose broken; zero local value | **DROP / DEFER** |
| 5 | Embeddings adapter | Ship adapter, activate later (blocked) | **REWORK** — ollama IS feasible (400MB RAM); shipping dead code is dishonest | **DO IT RIGHT — install ollama + nomic-embed-text now** |

**Score: 3 approved (reordered + rescoped), 1 dropped, 1 reworked.**

---

## The decisive finding (verified by me, then the Challenger)

The single most important discovery of the debate, which invalidates the original plan's #1:

```
src/uap/slice/orchestrator.py:97-107
  GraphNode(id="recon",    kind=AGENT, config={"agent": "echo"})
  GraphNode(id="summarize", kind=AGENT, config={"agent": "summarize"})
  GraphNode(id="fetch",    kind=TOOL,  config={"tool": "echo", ...})
```

**The slice graph hardcodes echo/summarize.** The Architect's plan — "register `LLMAgent` in the registry, ~120 lines, S effort" — would have produced a **no-op**: the registry would hold a real LLM agent that no node ever asks for. The platform would still echo. This is exactly the class of bug that makes "wired" features hollow.

Making the slice produce real AI output requires:
1. `build_research_graph()` must reference `"llm"` in at least one AGENT node (the "recon" node is the natural candidate)
2. **Failure resilience**: the linear graph has ZERO error edges — `runtime_adapter.py` raises `NodeExecutionError` on a failed agent, and with no ERROR edges, **one LLM failure halts the whole pipeline**. With a real LLM (network flakiness, rate limits), this WILL happen. Either add ERROR edges or make the LLM node degrade to echo on failure.
3. A determinism decision: the default test suite must not hit the network → LLM node behavior must be injectable/mocked in tests, live behind `UAP_LIVE=1`

**Effort: M/L, not S.** The Challenger's "dishonest estimate" call was correct.

---

## Point-by-point final decisions

### 1. LLMAgent — APPROVED, RESCOPED (do first)
- New: `src/uap/models/client.py` (`ChatClient` over httpx → `http://127.0.0.1:20128/v1/chat/completions`), `src/uap/agents/llm.py` (`LLMAgent(BaseAgent)`)
- Modified: `build_research_graph()` — recon node `agent: "llm"`; **add an ERROR edge or graceful fallback** so LLM failure doesn't kill the pipeline
- Config: `UAP_LLM_BASE_URL` / `UAP_LLM_API_KEY` env (never hardcode the token — Challenger flagged this)
- Tests: mocked by default; live behind `UAP_LIVE=1`
- Verified facts that make this safe: 9router is UP (745 models, chat completions verified working); `BaseAgent.run()` already converts exceptions to failed results; `runtime_adapter` re-raises FAILED as NodeExecutionError (hence the ERROR-edge need)

### 2. MCP wiring — APPROVED (do third)
The Challenger **verified the framing compatibility** by reading both codebases:
- mcp-go `server.ServeStdio` reads via `ReadString('\n')`, writes `fmt.Fprintf('%s\n')` 
- UAP client `_send()` does `json.dumps() + '\n'`, `_pump_stdout` reads lines
- **Both newline-delimited JSON — compatible.** The architect's unstated worry was unfounded.
- `StdioMCPClient(timeout_s=20.0)` — a hung bbmcp fails after 20s, not forever
- Caveat to document: `start()` is synchronous; wrap in `asyncio.to_thread` if ever called from an async path
- bbmcp binary verified present and functional (15.7MB Go ELF, starts in stdio mode)
- Graceful degradation via try/except: keep the app booting when bbmcp is absent

### 3. CI — APPROVED (do fourth, after fixing pyproject)
Challenger's catch: **`pip install -e '.[dev]'` would NOT produce a working environment.** `pyproject.toml` declares only `pydantic>=2.9`; the code imports psycopg, fastapi, uvicorn, httpx — **none declared**. Both the architect and researcher missed this. Fix order: (a) complete `pyproject.toml` deps, (b) then CI.
- 3.15 rc: needs `allow-prereleases: true` (no stable 3.15 exists in Oct 2026)
- Two jobs: main (3.15) + langgraph (3.13, only `test_langgraph_runner.py`)
- Service container: `pgvector/pgvector:pg17` (pgvector required by migrations — verified)
- Drop `-x`: for an 839-test suite, seeing all failures is more useful

### 4. Containerfile — DROPPED
- `python:3.15-slim` **does not exist** (3.15 is rc2-only) — would need `python:3.15-rc-slim`
- docker absent; **podman compose is broken on this machine** (exit 3, verified) — the "one-line install" doesn't work here
- The user runs locally; zero immediate value; premature packaging
- **Defer until**: platform produces real output, a stable Python 3.15 base exists, and there's an actual second user

### 5. Embeddings — APPROVED, REWORKED (do second)
The Challenger's feasibility check overturns the "blocked" assumption:
- `nomic-embed-text` via ollama: **~274MB download, ~300MB RAM** (ollama ~100MB) — fits comfortably in 5.3GB available RAM, 326GB disk
- **The honest move: install ollama + pull the model now**, ship `APIEmbedder` that actually works, not dead code
- Keep `get_embedder()` factory: `HashingEmbedder` stays the deterministic test default; `APIEmbedder` activates via `UAP_EMBED_URL`
- Matryoshka truncation (768→384) is valid for nomic-embed-text — testable once installed

---

## Revised execution order (Challenger's, accepted)

```
1. LLMAgent + graph redesign (M/L)     ← the "skeleton with no organs" fix
2. ollama + nomic-embed-text + APIEmbedder (S, real integration)
3. MCP wiring (S, verified compatible)
4. pyproject deps + CI (S/M, must follow #1-3 or CI fails)
5. ~~Containerfile~~ DROPPED
```

Rationale for the reorder: the original order put CI before working features. CI locks in behavior — but there was nothing worth locking in yet (the flagship demo echoed). Make the platform **do** something real first; then guard it.

---

## Items BOTH the architect and researcher missed (Challenger's list, all valid)

1. **pyproject.toml dependency gap** — only pydantic declared; psycopg/fastapi/uvicorn/httpx all imported but undeclared. Fresh installs and CI break.
2. **Hardcoded 9router bearer token** — surfaced in the researcher's report; must be an env var, never committed.
3. **Two-venv complexity is a product defect** — any CI/container/contributor must solve it. Consolidation (single 3.13 venv — the full suite passed 415/415 there earlier) is a real option.
4. **The platform can't demo its flagship feature without PostgreSQL running** — the §71 slice fails on connection refused. An in-memory/SQLite fallback for demo mode was never proposed.
5. **Pipeline failure resilience** — no ERROR edges in the linear graph; one failed node halts everything. Not analyzed by either.
6. **Cost/quota risk** — live tests against 9router's 745 models must be gated (`UAP_LIVE=1`), or the suite burns quota on every run.

---

## Bottom line

The debate changed the plan materially:

- **Point 1 as originally written would have shipped a no-op** — the flagship fix (LLM in the slice) needs graph surgery and failure handling, not agent registration.
- **Point 4 (Containerfile) should not be built** — it cannot work on this machine today.
- **Point 5 is not blocked** — ollama makes it a ~30-minute real integration instead of dead code.
- **A hidden blocker (undeclared deps) would have broken CI on first run.**
- **Priority inverted**: make it real (1, 2, 5) before making it reproducible (3), and don't package (4) until someone else wants it.

The Challenger's harshest line stands as the summary: *"The platform's flagship demo produces deterministic string concatenation."* Points 1 + 2 + 5, done as rescoped here, are what change that sentence.

---

## Execution results (added 2026-10-01, after running the revised plan)

| Step | Commit | Result |
|---|---|---|
| Deps fix | 197cd17 | All runtime deps declared; fresh install verified |
| 1. LLMAgent + graph | dda1191 | recon node → `"llm"`; agent-level PARTIAL degradation (ERROR edges rejected — engine's `_normal_ready` kills the node via the dead fetch→summarize DATA edge anyway); live 9router verified: LLM text lands in `slice-recon` artifact |
| 2. ollama + embedder | 495b651 | ollama v0.24.0 + nomic-embed-text (274MB) installed; **found + fixed a second hollow wiring**: slice knowledge items stored `embedding=NULL` (vector search always fell back to ILIKE). Now real 384-dim vectors; cosine search verified (dist 0.5869) |
| 3. MCP wiring | 5c4c806 | Real bbmcp: 11 tools registered, `list_agents` round-trips, subprocess reaped on shutdown; graceful degradation on missing binary |
| 4. CI | 57fe20b | Validated against a fresh DB: migration chain applies, 863→871 passed, langgraph job 14 passed |
| 5. Containerfile | — | Dropped as agreed |
| Extra (found during verification) | f66d065 | **Third hollow wiring**: the server's `POST /tasks` research path was still 100% stub ("Python is a programming language"). `ResearchWorkflow` now appends a real LLM `## Analysis` section; live-verified 4.8KB report |

**Test suite: 839 → 871 passed** (+32 new tests). Three "wired" features were found to be hollow during execution — each one only surfaced by running the actual path and reading the actual output, exactly the failure mode the Challenger predicted.

The Challenger's summary sentence is now false: the flagship demo produces real LLM analysis, backed by real embeddings, with a real MCP tool surface — all three verified live.
