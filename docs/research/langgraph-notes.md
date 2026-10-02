# LangGraph Research Notes (for UAP build Step 6)

Verified by execution on this machine (throwaway venv `/tmp/lgtest`, Python 3.13) plus official docs. Date: 2026-10-01.
Purpose: Step 6 (LangGraph Integration) needs our plain-asyncio contracts (built in Step 1) to be swap-ready. This note records the exact API we must target.

## 1. Versions (verified by install)

| Package | Version resolved |
|---|---|
| `langgraph` | **1.2.12** |
| `langgraph-checkpoint` | 4.2.0 |
| `langgraph-checkpoint-sqlite` | 3.1.1 |
| `langchain-core` | 1.6.6 (pulled automatically; needed by langgraph 1.x) |
| `langgraph-prebuilt` | 1.1.0 |
| `langgraph-sdk` | 0.4.5 |
| `aiosqlite` | 0.22.1 (transitive, for async SQLite) |
| `sqlite-vec` | 0.1.9 (transitive) |

Install: `pip install langgraph langgraph-checkpoint-sqlite`.

**Important environment constraint:** the UAP project venv runs **Python 3.15**, and LangGraph's deps (`ormsgpack`, `zstandard`) have **no wheels for 3.15** — the build fails. On Python 3.13 install works. Options for Step 6: (a) keep UAP on 3.15 and add a separate 3.13 venv only for the LangGraph-backed orchestrator process, (b) move the project venv to 3.13, (c) wait for 3.15 wheels. Decide at Step 6; Steps 2–5 are pure-asyncio and unaffected.

## 2. Core API surface (langgraph 1.2.x)

- `from langgraph.graph import StateGraph` — build with `StateGraph(StateSchema)`; `add_node(name, fn)`, `add_edge(a, b)`, `add_conditional_edges(a, router, mapping)`, `set_entry_point`/`START`/`END`, then `compile(checkpointer=...)`.
- Nodes are sync or async functions `(state) -> partial_state_update`; the graph is typed by a `TypedDict`/Pydantic state schema (reducers via `Annotated`).
- `from langgraph.types import interrupt, Command`:
  - `interrupt(value, *, response_schema=None) -> Any` — call inside a node to pause; the value surfaces to the caller.
  - `Command(*, graph=None, update=None, resume=None, goto=())` — resume a paused graph: `graph.invoke(Command(resume=...), config)`.
  - **This maps directly to UAP `ApprovalRequest` states** (pending_approval → approved/rejected) from Master section 20.
- Streaming: `graph.astream(input, config, stream_mode=...)` — `values`, `updates`, `messages`, `custom`, `debug` are the standard modes (use for UAP Observability, Step 15).
- Subgraphs: a compiled graph can be added as a node in another graph (`add_node("research_deep", deep_graph)`), sharing state keys — the pattern for embedding the Step-17 Deep Agent inside ResearchWorkflow (Master sections 7, 14).

## 3. Checkpoint / resume (Master section 21)

- `from langgraph.checkpoint.sqlite import SqliteSaver` (verified importable).
  - `SqliteSaver(conn: sqlite3.Connection, *, serde=None)` — wraps a plain `sqlite3.Connection`.
  - `SqliteSaver.from_conn_string(path)` classmethod exists (context-managed).
  - Also present: `AsyncSqliteSaver` path via `aiosqlite` (module has async variants in 3.1.x; verify exact name at Step 6 if async needed).
- Resume semantics: compile with `checkpointer=saver`, then every `invoke` carries `config={"configurable": {"thread_id": "<task_id>"}}`. Re-invoking with the same `thread_id` continues from the last checkpoint. `get_state(config)` reads the checkpoint; `get_state_history(config)` walks it.
- Implication for UAP: our `WorkflowState` (Step 1 contract) already carries `task_id`, `status` (incl. `checkpointed`), `current_node`, `node_history`, `version`. Map `task_id` → `thread_id`, `current_node` → next node to execute, `data` → LangGraph channel values. Keep those fields authoritative so the swap is a thin adapter.

## 4. Interrupt / human-in-the-loop (Master section 20)

- Call `interrupt({...payload...})` inside a node → the run pauses and returns control (with `__interrupt__` in the result when using `invoke`).
- Resume: `graph.invoke(Command(resume=<decision>), config)` with the same `thread_id`.
- Requires a checkpointer (state must persist across the pause).
- Implication: UAP `ApprovalRequest` (Step 1) is the external contract; LangGraph `interrupt` is the mechanism at Step 6+. The workflow layer, not the agent, owns the transition.

## 5. UAP integration implications (what Step 1 contracts must expose — already true)

| UAP contract field | LangGraph mapping | Status |
|---|---|---|
| `WorkflowState.task_id` | `config.configurable.thread_id` | present |
| `WorkflowState.status` (`checkpointed`, `awaiting_approval`, ...) | pause/resume points | present |
| `WorkflowState.current_node` + `node_history` | resume point | present |
| `WorkflowState.data` (dict) | channel state | present |
| `WorkflowState.version` | optimistic concurrency on writes | present |
| `ApprovalRequest.state` | `interrupt` + `Command(resume=...)` | present |
| `VerificationResult` | gate node before `END` | present |

Conclusion: no contract changes needed for a clean Step-6 swap. The Step-6 work is: (1) resolve the Python-version constraint, (2) write a `LangGraphWorkflowRunner` adapter implementing the same runner interface as the Step-2..5 asyncio runner.

## 6. Unverified / follow-ups

- Exact `AsyncSqliteSaver` symbol name in `langgraph-checkpoint-sqlite` 3.1.1 (module listing showed sync `SqliteSaver`; async variant to confirm at Step 6).
- `stream_mode="messages"` requires `langchain-core` message objects — confirm when wiring Observability (Step 15).
- LangGraph 1.2 API for retry policies (`RetryPolicy`) — not inspected; may be useful for Step 12 (Verification) retries.
