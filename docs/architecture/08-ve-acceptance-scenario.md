# Visual Operating Environment — 16-step acceptance scenario

Run in a real Chromium against `uvicorn uap.server.app:create_app --factory --port 8023`.
Every step must be VERIFIED against the live server, not assumed.

| # | Step | Pass condition |
|---|------|----------------|
| 1 | Create workspace | POST /api/workspaces -> appears in the selector; creating one twice with the same name is handled honestly |
| 2 | Enter task | Canvas input accepts text; "Generate proposal" is the primary action |
| 3 | Generate workflow proposal | POST /api/proposals returns domain + workflow + graph; clarification case shows the inline answer flow (no alert) |
| 4 | Show graph before execution | Canvas renders the PROPOSED graph (real nodes from runner.nodes) BEFORE any run exists; no run record created |
| 5 | Inspect agent | Clicking an agent node shows its real config + referenced agent in the inspector |
| 6 | Inspect tool | Library > Tools lists real tools (risk tiers); clicking shows schema |
| 7 | Execute | "Run" creates the task; the graph switches from proposed to executing |
| 8 | Graph shows live execution state | Node statuses advance (running -> completed) as events arrive; no fake statuses |
| 9 | Pause execution | Pause takes effect between nodes; run status becomes "paused" in the UI |
| 10 | Resume execution | Run completes after resume |
| 11 | Inspect event stream | Bottom console shows the real canonical events for the run |
| 12 | Inspect artifact | Artifacts panel lists report.md/sources.json; content viewer shows the real text |
| 13 | Inspect provenance | Knowledge item (if any) shows provenance; artifact shows source + evidence_source badge ("deterministic-stub evidence") |
| 14 | Reconnect browser | Hard reload; the app resyncs from REST + WS without losing the run |
| 15 | Runtime continues | The run (started before reload) is still tracked server-side and completes |
| 16 | UI resynchronizes | After reload the graph shows the correct final statuses for the same task |

## Anti-requirements (any failure here is a REJECT)

- `/api/executions/{id}/graph` must NEVER return the old 3-node placeholder ("User Input" -> "Agent Core" -> "Result"). Grep the source: `grep -n "User Input" src/uap/server/app.py` must be empty.
- Every declared graph node id must exist in `workflow.runner.nodes` (test-enforced).
- No alert() in ui/index.html.
- Stub evidence must be labelled as such (evidence_source badge).
- No fake data rows in any list when the backend returns empty/404.

## Execution log

(Fill during verification — one line per step with the observed evidence.)
