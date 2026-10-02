# Night Session Handover

Date: 2026-10-03 (overnight) · Status: **all work committed and pushed** (`f9094ef`)

---

## TL;DR

Four workstreams. The most important: **the bug bounty workflow now performs
real reconnaissance** — it was silently doing nothing. Suite: **1198 passed,
5 skipped**.

---

## 1. Bug bounty was doing NOTHING (and said it succeeded)

A `bug bounty on example.com` run reported `completed` while performing zero
reconnaissance. Two silent configuration gates:

| Gate | Problem |
|---|---|
| Binary path | `src/uap/mcp/config.py` hardcoded `/home/user/bugbounty-mcp/bbmcp` — a developer machine path. The real binary was at `/home/amen/bugbounty-mcp/bbmcp` and worked (15 MB, 11 tools). |
| Enable flag | `UAP_MCP_ENABLED` had to be set or MCP was skipped entirely. Nobody knew to set it. |

**Fixed:** the path is DISCOVERED (`UAP_BBMCP_BIN` → PATH → sibling checkout →
`$HOME` → legacy literal), and MCP now AUTO-DETECTS: an explicit param wins,
`UAP_MCP_ENABLED=0` forces off, otherwise it starts when the binary exists.

**Verified end to end** — real MCP calls now appear:

```
mcp_call | bugbounty-mcp.find_subdomains_passive | domain: example.com
mcp_call | bugbounty-mcp.probe_http              | url: https://example.com
```

and the subdomains reported (`dev.example.com`, `m.example.com`) **match crt.sh
for that domain exactly** — cross-checked against the live crt.sh JSON.

### The honesty label was lying
`RunRecord.evidence_source` was hardcoded to `"deterministic-stubs"` and never
updated, so a run that really queried crt.sh told the user its findings were
fixtures. It now reports what the run actually did: `mcp:2-calls`,
`mcp-available-no-calls`, or `deterministic-stubs` only when true. For a
security tool, real-vs-fixture is the whole product.

---

## 2. Results were attributed to the WRONG RUN (data leak)

Found and fixed earlier in the session; recorded here because it is the most
serious defect found:

```
BEFORE:  Alice asks "research ALICE project" -> receives BOB's report
         Bob asks "research BOB roadmap"     -> receives ALICE's report
AFTER:   each receives their own
```

`run_once()` claims the OLDEST pending row while a synchronous caller waits on
the row it just created. Fixed with `Worker.run_specific` / `claim_by_id`;
`run_once` keeps its queue-draining behaviour, pinned by a test.

---

## 3. The canvas is a stage, and nodes publish into it

- `ui/js/stage.js` hosts 8 view kinds; all verified rendering in a browser:
  html, markdown (own renderer), image (`naturalWidth=600`), video
  (`readyState=4`, 960x540), iframe, code, **whiteboard (real Excalidraw — 2
  canvases, no X-Frame-Options)**, placeholder (an unknown kind says so).
- A node publishes `config["view"]`; the synthesis node publishes one
  automatically from its real output. The view is attached as `value["view"]`
  on the result — the result is not replaced — and persisted to `node_views`,
  so it survives a reload.
- `GET /api/executions/{id}/views` accepts EITHER the row id or the
  correlation id, returning identical payloads (verified on the same live run).

### Side panes collapse (your last request)
With a 280px sidebar and a 420px inspector, the work area was ~55% of the
screen. Both panes now collapse to an edge handle (`›` / `‹`) that restores
them on click, plus `Ctrl+B` / `Ctrl+I`.

Measured: `280 + 420 + 868` → `0 + 0 + 1568` (full width).

---

## 4. A run now records who asked for it

A persona review: *"there is no user identity anywhere, so no action can be
attributed to a person, which alone fails procurement."*

- `executions.requested_by` (nullable), migration `e7a1b4c9f2d3`, applied to
  both `uap` and `uap_test`.
- Path: `POST /tasks` body → RunRecord → slice → `service.enqueue` → column →
  `GET /tasks` / `GET /tasks/{id}`.
- `GET /tasks?requested_by=<id>` filters to one person's runs.

Verified independently: alice → 1 task, bob → 1 task, charlie → 0; an omitted
`user_id` stores NULL rather than a bogus default.

**Honest limitation** (in the docstring): this is ATTRIBUTION, not
authentication. A caller can claim any id, so it is not a security boundary.

---

## Provider status (why agents kept dying)

| Provider | Status |
|---|---|
| `vs/*` (vsllm) | 🔴 **balance ¥0.00** — killed every agent mid-run |
| `AG/*` (AgentRouter) | ⚠️ **running** (proxy service enabled at 8318) but the account has a channel for **1 model only**: `AG/deepseek-v4-flash`. 15 others return `无可用渠道` ("no channel in group default"). |
| `ag/*` (Antigravity) | ✅ 4 models working — gemini-3.8-flash-high, claude-opus-4-6-thinking, claude-sonnet-4-6, gemini-3.7-flash-high |
| `oc/*`, `cbai/*`, others | ✅ 14 models verified alive |

All 9 project agents now carry an **18-model fallback chain**, every entry
probed alive and 1M+ context before being written down.

**AgentRouter note:** the proxy works; the account is the limit. The 503 models
(claude-opus-5, claude-opus-4-8, gpt-6-astra) may return after the quota pool
resets (~09:00 and ~18:00 WIB, per your note). The `NO_CHANNEL` ones will not
appear without access changes.

---

## Open items

1. **Learning domain does not exist.** You want video + transcript + notes +
   sources + whiteboard on the canvas. The stage can now render all of that,
   but no learning workflow produces it. This is the biggest remaining gap
   between what you asked for and what runs.
2. **Bug bounty autonomy is a setting, not behaviour.** Full/semi choice was
   requested; only the plumbing exists.
3. **Multi-user is still single-operator.** Attribution exists; accounts,
   roles, and shared workspaces do not. `POST /api/workspaces` works now
   (fixed), but there is no member model.
4. **No provider integration.** HackerOne needs your API key (401 without it);
   YesWeHack's program list is public (200); Immunefi's page loads. Nothing in
   the codebase reads a program list yet.
5. **Pause does not stop a running execution** — it is checked between nodes,
   and stub nodes take 0 ms, so the run finishes anyway (persona finding,
   measured: paused at 8ms, completed at 621ms).
