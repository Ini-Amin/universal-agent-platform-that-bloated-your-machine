# UAP — Product Context

## What it is

Universal Agent Platform (UAP) is a **visual operating environment for building, executing,
observing, debugging, evaluating, and evolving agentic systems**. It is not a chatbot, not a
research demo, and not a backend architecture rendered inside a webpage.

The central product object is the **Workspace**. The central interaction surface is the
**visual workflow canvas**. The central computational abstraction is the **agentic workflow**.

## Who it is for

- **Primary:** a single technical operator (developer / security researcher) running UAP
  locally, who wants to compose, run, and inspect agent workflows without leaving a visual
  surface.
- **Situation:** working at a desk, in a long-lived session, on a machine that also holds
  their credentials and network access.
- **Job:** turn a natural-language task into an executable workflow, watch it run, and be able
  to inspect exactly what happened (which node, which tool, which model, which artifact) and
  intervene (pause, resume, approve) when something needs a human decision.

## What it makes possible

| Capability | Mechanism |
|---|---|
| Task → executable workflow | Entry workflow (intent) → Router (domain) → workflow proposal shown on the canvas **before** execution |
| Durable execution | PostgreSQL-backed executions, events, decision traces, checkpoints; a run survives a browser reload and a server restart |
| Real agent work | Research pipeline nodes (analysis → planning → fan-out → extraction → filtering → verification → synthesis → review) executed through the canonical graph |
| Real security recon | BBP workflow calling real MCP tools (`bugbounty-mcp.*`) behind a fail-closed scope gate |
| Human control | Approval gate for side-effectful tools; pause/resume; per-node live status on the canvas |
| Honest evidence | Stub/deterministic output is labelled `SIMULATION` in every artifact; real tool runs are not |

## Durable constraints (must be preserved)

1. **The canvas is the primary workspace.** It renders the canonical graph from the backend —
   never a decorative or frontend-invented graph. No placeholder graphs.
2. **Definition ≠ execution.** A workflow definition is what it is; an execution graph is what
   happened. Runtime mutation never silently rewrites the definition.
3. **Never present mock data as real.** Deterministic stub evidence carries the SIMULATION
   banner; real-tool output does not. This is a product-integrity rule, not a styling choice.
4. **The scope gate runs first.** For BBP, out-of-scope targets must never reach a tool call.
5. **Governance is enforced outside LLM reasoning.** Policy, capability, credential, and
   approval checks run at the tool-call choke point, not in a prompt.
6. **Local-first.** Single user, no mandatory auth, OS-level access boundary, no multi-tenancy.
7. **The architecture must be visible in the product.** Agents, tools, skills, knowledge,
   policies, models, MCP, artifacts, and traces are inspectable resources — not hidden
   implementation detail.

## Platform

`web` — served by the FastAPI server as plain ES modules (no build step, no framework).

## Stack

- Backend: Python 3.12+ / FastAPI / Pydantic / SQLAlchemy / Alembic / PostgreSQL + pgvector
- Frontend: plain ES modules + CSS (deliberately no framework; the canvas is hand-rendered SVG)
- Orchestration: canonical typed graph + `GraphExecutor`, with an optional LangGraph adapter
- Integration: MCP (stdio), OpenAI-compatible model endpoints

## Terminology (use consistently in UI copy)

| Term | Means |
|---|---|
| **Task** | what the user asked for (natural language input) |
| **Workflow** | a definition: the graph of nodes and edges |
| **Run / Execution** | one execution of a workflow; what actually happened |
| **Node** | one step in the graph (agent, tool, synthesis, evaluation, knowledge, approval, ...) |
| **Artifact** | a produced object (report, findings, dataset, ...) |
| **Resource** | an inspectable library item: agent, tool, skill, model, policy, MCP server |

## Open decisions

- Authentication: currently none (local-first assumption). A single-token gate is the planned
  minimum before any non-local exposure.
- Real research collectors: the web/paper/document collectors are deterministic stubs today;
  replacing them with real fetchers is planned but not done.
