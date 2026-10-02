# UAP — 12-Phase Build Report (Revision Spec Adoption Complete)

Generated 2026-10-01 by the orchestrator. Final state of the Universal Visual Agent Orchestration Platform build.

## Final verification (all gates green)

| Gate | Result |
|---|---|
| Full suite (Python 3.15 venv, serial) | **838 passed, 1 skipped** |
| LangGraph suite (Python 3.13 venv) | **14 passed** |
| §71 vertical slice E2E (independent run) | **completed** — 7 artifacts, 1 knowledge item, 19 events, eval passed, 2nd run reused `research-slice@v1` |
| Alembic | single head `b7f1c2a9d4e6`, 3-revision chain clean |
| Git | tree clean, 20+ commits this session |
| Module count | 151 Python modules, 42 test files, 838 tests |

## What was built (12-phase plan mapping)

| Phase (§70) | Delivered | Key modules |
|---|---|---|
| **0 — Foundation** | PostgreSQL 18.3 + pgvector 0.8.0, SQLAlchemy 2, Alembic, versioned definitions, durable events | `uap.db` (10+ tables) |
| **1 — Workspace + Library** | Workspace entity + deterministic detection (never silently switches), versioned Global Library with exact `name@vN` refs | `uap.workspace`, `uap.library` |
| **2 — Workflow Engine** | Canonical typed graph (§55), validator (ports/conditions/joins/subworkflow refs), graph executor (parallel, conditions, fan-in policies, nesting, approvals) | `uap.graph`, `uap.execution` |
| **3 — Agent System** | Agent protocol + registry, Tool Registry (tiers 0–3), Skill system, MCP stdio adapter, Model Router | `uap.agents`, `uap.tools`, `uap.skills`, `uap.mcp`, `uap.models` |
| **4 — Policy + Capability** | Capability resolver + scoped HMAC tokens, Allow/Ask/Deny policy engine, credential manager (refs only), sandbox guard | `uap.capability`, `uap.policy`, `uap.credentials`, `uap.sandbox` |
| **5 — Durable Runtime** | PG-backed executions, restart-safe worker with heartbeats, checkpoints/resume/fork/pause, canonical event store (29 event types), decision traces | `uap.runtime`, `uap.events`, `uap.trace` |
| **6 — Visual Runtime** | 3-pane canvas IDE (§52), canonical-graph renderer (§55), execution overlay, WebSocket transport with resync (§44), 4-store state (§54), undo/redo with AI/user actor tags (§56) | `ui/`, `uap.server.ws` |
| **7 — Context + Knowledge** | Context compiler (caps, relevance), knowledge layer with pgvector search + provenance + policy-gated lifecycle | `uap.context`, `uap.knowledge` |
| **8 — Multi-Agent + Dynamic** | Message bus (control plane), team formation, delegation with depth guard, agent trust scores, dynamic graph mutation (validated, audited), subworkflow resolver with recursion guard | `uap.coordination`, `uap.trust`, `uap.dynamic`, `uap.subworkflows` |
| **9 — Events + Integration** | Canonical event model, git integration (definition export/commit/diff), compatibility system | `uap.events`, `uap.gitx` |
| **10 — Evaluation + Learning** | Versioned evaluator entities, closed-loop proposals (never auto-applied), reproducible packages with integrity verification | `uap.evaluators`, `uap.learning`, `uap.packages` |
| **11 — Resource Orchestration** | Scheduler with quotas + backpressure, error classifier + recovery policy | `uap.scheduler`, `uap.recovery` |
| **12 — Advanced Runtime** | Fault injection (4 kinds), execution replay with provenance — container/remote runtime deferred (honest scope) | `uap.faults`, `uap.replay` |

Plus the **§71 vertical slice**: `uap.slice` — one complete path: Entry → Workspace Detection → Existing-Workflow Detection → versioned Workflow → Agent → Tool → Context → durable Execution → Event Stream → Decision Trace → Artifact → Knowledge → Evaluation → Git.

## Architectural invariants held (test-enforced)

- Definition ≠ execution: mutations never touch running definitions (§2.3) — verified by tests.
- Versioning: `get_version(id, 1)` byte-identical after v2 exists — verified live.
- Fail-closed security: scope gates block before any action; policy engine defaults to ASK; sandbox blocks path escapes.
- Closed loop never auto-applies: proposals require approval; `apply()` raises unless approved.
- No God objects: Entry routes only, Router never plans, agents never import UI/DB/MCP/LangGraph.
- Determinism: every module has deterministic tests; no network in the suite.

## Honest gaps / deferred

1. **Container/remote runtime** (§30 phase 12): sandbox is an in-process call-boundary guard, not an OS sandbox — documented in `uap/sandbox/executor.py`.
2. **WebSocket push**: the UI polls `events_since` every 0.5s rather than worker-pushed — documented in `uap/server/ws.py`.
3. **Knowledge promotion** may be policy-denied at default confidence 0.6 — the slice tolerates denial and still satisfies `knowledge_eligible`.
4. **Live LLM agents**: all agents are deterministic stubs; the Model Router is wired but no real model calls are made (no network policy in tests).
5. **Test isolation**: DB-backed test modules share `uap_test`; run the suite serially (parallel runs cause spurious FK failures).

## Session commits (this build)

`3b133e1` persistence · `5bc6ae2` workspace+library · `41ae5bb` graph · `86f1f2e` model router · `2a2ccee` events+trace · `1d9a346` gitx · `7109623` executor · `a0256c0` runtime · `01bcd45` knowledge · `6e29e26` security · `d034e97` eval+learning+packages · `c39420b` coordination · `ac5eee6` scheduler+recovery+faults+replay · `65a6250` canvas UI · `dec09ea` vertical slice

## How to run

```bash
# Server + new canvas IDE
.venv/bin/uvicorn uap.server.app:create_app --factory --port 8000
# → http://127.0.0.1:8000/ui/   (canvas IDE, live events)
# → http://127.0.0.1:8000/      (original simple UI, still works)

# Tests
.venv/bin/python -m pytest -q                                   # 838 passed
.venv-langgraph/bin/python -m pytest tests/test_langgraph_runner.py -q   # 14 passed

# The §71 slice, programmatically
python -c "
from uap.slice import PlatformSlice
r = PlatformSlice().run('research distributed consensus')
print(r.execution_status, len(r.artifacts), r.knowledge_ids)"
```
