# Build Plan - Universal Agent Platform

Source of truth: `<path-to>/Universal Agent Platform - Master.md` ("Master section N").
Repo: `/home/user/universal-agent-platform` - Python 3.15 venv - Infra now: SQLite + filesystem (upgrade path per Master section 25: PostgreSQL + pgvector, Redis).
First domain (proof of architecture, Master section 26): **Research**.
Build order: Master section 26, steps 1 to 17. Checkpoints C1-C3 pause for human review.

## Agent delegation

The orchestrator delegates each build step to a specialized agent (see the
`agents/` definitions for the harness). Model selection is environment
specific — set `UAP_MODEL_CATALOG` / `UAP_DEFAULT_MODEL` to match whatever your
gateway exposes. The platform does not hardcode provider names.


## Checkpoints

- **C1** after Step 1: contract freeze review.
- **C2** after Step 4: first domain workflow review.
- **C3** before external-service steps (8 MCP, 15 observability).

## Progress

- [x] Scaffold: repo, venv, pyproject, docs (6d9ec9f)
- [x] Step 1 - Core Contracts: 13+4 models (2a05e27) + hardening to 108 tests (26f1288)
- [x] Research notes: LangGraph 1.2.12 verified + bugbounty-mcp inventory (9a4d406)
- [x] **C1 review passed**
- [x] Step 3 - Router: data-driven map, 13 tests (c98a797)
- [x] Step 9 - Skill System: registry + loader + 3 library skills, 17 tests (72daeee)
- [x] Step 15 - Observability: event bus + JSONL sink + tracer, 13 tests (8668b0f)
- [x] Step 6 prep: .venv-langgraph (Python 3.13 side-venv, langgraph 1.2.12 verified) (d3af5c4)
- [x] Steps 2, 3, 4, 5, 7, 13 (core flow + infra)
- [x] Steps 8, 9, 10, 11, 12, 15 (infra + surface)
- [x] Steps 6, 14, 16, 17 (final wave)
- [x] E2E integration test (orchestrator-owned)
- [x] Final verification: 401 passed (main venv) + 14 passed (langgraph venv)
- [x] Build report: docs/architecture/02-build-report.md

## BBP domain (post-17 addition, Master section 30 proof)

- [x] BBPWorkflow + deterministic fail-closed ScopeGate (19 tests) — 7c817ab
- [x] Entry: target/scope extraction + bbp keyword rebalance (34 tests) — bbf8460
- [x] Server multi-domain dispatch + per-request scope gate (23 server tests) — 3454e6f
- [x] Live verification: screenshot input -> completed bbp run; excluded target -> failed closed
- Total: 441 tests passing (main venv)

The §30 promise held: adding a domain cost one workflow + entry extraction + server wiring. No changes to contracts, router architecture, tool registry, context engine, memory, artifacts, or observability.
