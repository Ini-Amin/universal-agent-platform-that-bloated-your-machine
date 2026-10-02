# Honest Cross-Review: luvus (external) vs universal-agent-platform (ours)

Generated 2026-10-01. Method: two independent review agents (one per repo, no shared context) + orchestrator crosscheck of the three most damning self-audit claims. Every claim below was verified by grep/count, not taken on faith.

---

## TL;DR — the honest answer

**They are different products solving adjacent problems, and luvus is the more mature, more real piece of software.**

- **luvus** is a *terminal multiplexer / mission control for coding agents* — a polished, shipping, single-binary Rust tool with 947 stars, 35 releases, multi-platform CI, and a real user base. It does one thing deeply: manage live agent sessions in terminals.
- **ours** is an *agent orchestration platform* — a Python platform with a genuinely impressive breadth of architectural concepts (versioned definitions, graph execution, durable runtime, decision traces, knowledge layer, policy engine), all internally integrated and tested — **but with zero connection to the outside world.** No LLM calls, no real MCP wiring, no neural embeddings, no users, no CI, no distribution.

**The uncomfortable truth from our own audit:** our platform is a **complete, well-tested skeleton with real internal plumbing and no external organs**. Every boundary is stubbed.

---

## 1. Scale & maturity (verified numbers)

| | luvus | ours |
|---|---|---|
| Language | Rust (~150k LOC src; ~220k with vendored terminal engines) | Python (22,886 LOC src) + 1,791 LOC JS/CSS UI |
| Source files | ~256 Rust files | 151 modules |
| Tests | ~380 `#[test]` targets, 250KB+ dedicated test code | 839 pytest tests (741 without PG), 15,065 LOC test code |
| Releases | 35 (v0.1.1 → v0.14.3), crates.io published | 0 (local repo, pushed today) |
| Stars / forks | **947 / 64** | 0 / 0 |
| Contributors | 9+ (lead: 479 commits) | 1 (built in one session by orchestrator + subagents) |
| Commit cadence | 169 commits in last 4 weeks | 55 commits in ~9 hours (one session) |
| CI | clippy `-D warnings`, cargo audit, binary-size budgets, macOS/Windows/Linux/Nix matrix | **none** |
| Docs | 38 structured MDX docs + website (luvus.dev) | 5 markdown docs in docs/ |
| Distribution | `curl \| sh`, Homebrew, PowerShell installer, `luvus update` | `git clone` |

**Verdict on scale:** luvus is ~7x the LOC and has a real delivery pipeline. Ours was built in one session — which is remarkable as a feat, but it is not the same as shipping.

---

## 2. Feature matrix (cross-checked, evidence-based)

| Capability | luvus | ours | Notes |
|---|---|---|---|
| **Workflow graph execution + checkpoints** | ❌ partial — flat task ledger with deps (`src/orch/mod.rs`), no DAG engine, no checkpoint/rollback | ✅ **SOLID** — real GraphExecutor with parallel/fan-in/conditions + PG checkpointing, `SELECT FOR UPDATE SKIP LOCKED` leasing, crash recovery | **We win this clearly.** luvus orchestrates tasks; we execute graphs with durability. |
| **Versioned definitions** | ❌ partial — semver on module manifests only; agents are compiled-in, skills are static markdown | ✅ **SOLID** — 4 versioned resource pairs (workflow/agent/tool/skill), immutable version rows, content hashes, exact `name@vN` refs | **We win.** Their "versioning" is package semver, not resource versioning. |
| **PostgreSQL / durable DB** | ❌ absent — flat JSON/TOML files with atomic replace | ✅ **SOLID** — PG 18 + pgvector, 3 Alembic migrations, 10+ tables | **We win** on durability model. They win on zero-config (single binary, no DB needed). |
| **Decision traces** | ❌ absent — operational logs only | ✅ **SOLID** — structured traces with alternatives/evidence/confidence + secret redaction, wired into the slice | **We win.** |
| **Knowledge + vector search** | ❌ absent — fuzzy text search only | ⚠️ **THIN** — pgvector wired, lifecycle implemented, **but embeddings are SHA-256 bag-of-words, not neural** → semantically meaningless | Nobody wins. Ours is structurally complete but functionally fake. |
| **Evaluation / learning loop** | ❌ partial — task quality-gate commands | ✅ **SOLID** — versioned evaluators, closed-loop proposals with approval gates, packages | **We win** on concept; theirs is more practical (real gates on real work). |
| **Security layer** | ❌ partial — single-user local-trust, 0700 sockets, no OS sandbox | ⚠️ **THIN** — capability/policy/credentials exist, sandbox is honest in-process guard; policy not in default server path | Both weak. Ours has more surface, theirs has fewer promises. |
| **MCP support** | ❌ absent — proprietary UHP protocol instead | ⚠️ **THIN** — full stdio client + adapter + config **but never wired into any runtime path** (verified: zero callers outside `src/uap/mcp/`) | **We built it, we don't use it.** Honest verdict: dead code until wired. |
| **Web UI** | ✅ **present & real** — Axum + TS/Vite, QR pairing, opt-in control, shipping to users | ⚠️ **THIN** — 3-pane canvas IDE exists, works in-browser (we screenshotted it), but no e2e browser tests, needs PG | **They win.** Theirs is battle-tested by users; ours is a fresh prototype. |
| **Real agent/LLM integration** | ✅ **present** — detects, tracks, resumes, forks 20+ real agent CLIs | ❌ **absent** — EchoAgent/SummarizeAgent stubs; **ModelRouter has ZERO callers** (verified by grep); no LLM HTTP call anywhere | **They win decisively.** This is our biggest gap. |
| **Terminal/PTY mastery** | ✅ deep — cross-platform PTY, ConPTY, ANSI normalization, scrollback, detach/reattach | ❌ n/a — not attempted | Different domains. |
| **Remote/SSH workspaces** | ✅ present | ❌ absent (deferred phase 12) | They win. |
| **CI/CD + distribution** | ✅ exemplary | ❌ none | They win decisively. |

**Score by row (honest):** luvus wins 6, ours wins 4, 3 ties/thin-both.

---

## 3. The three claims I cross-checked myself (all confirmed)

The self-audit's harshest findings — I verified each with independent grep:

1. **"ModelRouter has zero callers"** → `grep -rn "ModelRouter" src/uap | grep -v src/uap/models/` = **empty.** Confirmed: 634 LOC of well-designed routing logic that nothing calls.
2. **"MCP adapter never wired"** → `grep -rn "register_mcp_server\|StdioMCPClient" src/uap | grep -v src/uap/mcp/` = **empty.** Confirmed: the bugbounty-mcp binary exists on disk, the client works against mocks, and no runtime path ever connects.
3. **"Agents are echo stubs"** → `orchestrator.py:187-188` registers `EchoAgent()` and `SummarizeAgent()`. Confirmed: the §71 slice "completes" by echoing deterministic text.
4. **"No real LLM call anywhere"** → grep for `openai|anthropic|httpx.post|api_key` outside tests/credentials = **only redaction comments.** Confirmed.

---

## 4. What each side should genuinely learn from the other

**What we should copy from luvus:**
- **Ship it.** 35 releases, installers, `luvus update`, crates.io — versus our zero distribution. A platform nobody can install is a library, not a product.
- **Connect to reality first.** They manage real Claude Code/Codex/Copilot sessions today. Our platform's Model Router and MCP client would be transformative *if wired* — but wiring them is the entire remaining job.
- **CI as a gate, not an afterthought.** Their clippy `-D warnings` + cargo audit + binary-size budget is production discipline. Our suite errors 17 tests when PG is down and has no workflow file.
- **Lean delivery.** ~3MB single binary, sub-10MB idle RAM. Ours needs Python + PostgreSQL + two venvs.

**What luvus could learn from us:**
- **Durable graph execution.** Their orchestration is a flat task ledger with attempts and worktrees — no DAG, no conditions, no checkpoint/rollback. Our executor does this properly, with crash recovery proven under concurrency.
- **Resource versioning.** Their modules have semver; their agents/skills are compiled-in or static. We version workflows/agents/tools/skills as immutable rows with exact refs — updating a definition can never silently change a running workflow.
- **Decision traces & secret redaction.** They log operations; we record structured decisions with alternatives, evidence, and recursive credential scrubbing.
- **Policy/approval as first-class states.** Their security is documented local-trust; ours models approval states and fail-closed gates explicitly (even if not yet fully wired).

---

## 5. The honest bottom line

**If you need to manage real coding agents in terminals today: use luvus.** It works, it's installed, 947 people agree, and it talks to 20+ real agent CLIs.

**If you're building an orchestration platform: our architecture is genuinely more advanced in 4 specific areas** (graph execution with durable checkpoints, resource versioning, decision traces, evaluation/learning loop) — and those are hard things that luvus does not attempt. But our work is **architecture without integration**: every external boundary is stubbed, nothing is distributed, nobody has used it.

**The single most important number in this review:** `grep -rn "ModelRouter" src/uap | grep -v src/uap/models/` → empty. We built a model router and never routed a model. Until that changes — until the platform actually calls an LLM, actually connects an MCP server, actually serves a real user — it is an exceptionally well-tested blueprint.

**Recommended next steps (in priority order):**
1. Wire `ModelRouter` → a real agent (`LLMAgent(BaseAgent)`) → make the §71 slice produce actual AI output
2. Wire `register_mcp_server(BUG_BOUNTY_MCP_CONFIG)` into server startup (binary already exists)
3. Add `.github/workflows/ci.yml` with PG service container
4. Ship a Dockerfile + one-line install so someone besides you can run it
5. Replace `HashingEmbedder` with a real embedding endpoint behind the existing `Embedder` protocol (one class swap)
