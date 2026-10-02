# Universal Agent Platform

A modular agent platform: you give it a task in plain language, it routes the task to a workflow
(research, bug bounty, ...), runs it through real agents, and saves structured artifacts —
with durable state in PostgreSQL, live events, and optional real LLM + embeddings.

> Every command in this README is copy-paste ready. One code block = one terminal paste.

---

## Step 0 — What you need

| Requirement | Why | Check with |
|---|---|---|
| Python 3.12 or newer | runs the platform | `python3 --version` |
| Git | to clone this repo | `git --version` |
| Docker Desktop | easiest database setup | `docker --version` |
| OR PostgreSQL 17+ with pgvector | database without Docker | see Step 2B |

Optional: [ollama](https://ollama.com/download) for real vector embeddings.

---

## Step 1 — Download the code

Works on any OS:

```bash
git clone https://github.com/Ini-Amin/universal-agent-platform-that-bloated-your-machine.git
```

Then go into the folder (this is the folder you stay in for every later step):

```bash
cd universal-agent-platform-that-bloated-your-machine
```

---

## Step 2 — Start the database

Pick **one** of 2A (Docker, recommended) or 2B (native PostgreSQL).

### Step 2A — With Docker (recommended, any OS)

```bash
docker run -d --name uap-pg -e POSTGRES_USER=uap -e POSTGRES_PASSWORD=uap_local_dev -e POSTGRES_DB=uap -p 5432:5432 pgvector/pgvector:pg17
```

Create the test database:

```bash
docker exec uap-pg psql -U uap -d uap -c 'CREATE DATABASE uap_test OWNER uap;'
```

✅ **You should see:** `CREATE DATABASE`

> Docker not running? Start Docker Desktop first. Port 5432 already used? You have another
> PostgreSQL running — either stop it, or use Step 2B with your existing server.

### Step 2B — Native PostgreSQL

#### macOS (Homebrew)

```bash
brew install postgresql@17 pgvector
```

```bash
brew services start postgresql@17
```

```bash
createuser -s uap && createdb -O uap uap && createdb -O uap uap_test
```

#### Ubuntu / Debian

```bash
sudo apt install -y postgresql-17 postgresql-17-pgvector
```

```bash
sudo -u postgres psql -c "CREATE ROLE uap LOGIN PASSWORD 'uap_local_dev' SUPERUSER;" -c "CREATE DATABASE uap OWNER uap;" -c "CREATE DATABASE uap_test OWNER uap;"
```

#### Fedora / RHEL

```bash
sudo dnf install -y postgresql-server postgresql-contrib pgvector
```

```bash
sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql
```

```bash
sudo -u postgres psql -c "CREATE ROLE uap LOGIN PASSWORD 'uap_local_dev' SUPERUSER;" -c "CREATE DATABASE uap OWNER uap;" -c "CREATE DATABASE uap_test OWNER uap;"
```

#### Windows

Easiest: use Docker (Step 2A). Or install PostgreSQL 17 from
[postgresql.org/download/windows](https://www.postgresql.org/download/windows/), add pgvector,
then in pgAdmin or psql run:

```sql
CREATE ROLE uap LOGIN PASSWORD 'uap_local_dev' SUPERUSER;
CREATE DATABASE uap OWNER uap;
CREATE DATABASE uap_test OWNER uap;
```

> The `uap` role needs `SUPERUSER` so it can run `CREATE EXTENSION vector` during setup.

---

## Step 3 — Create the Python environment

### Linux / macOS

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
```

✅ **You should see:** pip installs ~40 packages, ending with `Successfully installed ... universal-agent-platform`

### Windows (PowerShell)

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -e ".[dev]"
```

> **From now on, run every command with the venv active.** If you open a new terminal later,
> re-activate it with `. .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\Activate.ps1`
> (Windows). On Windows `python`/`pip` replace `python3`/`pip`.

---

## Step 4 — Prepare the database tables

```bash
alembic upgrade head
```

```bash
DATABASE_URL=postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test alembic upgrade head
```

Windows PowerShell (same thing, different env-var syntax):

```powershell
$env:DATABASE_URL="postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"; alembic upgrade head; Remove-Item Env:\DATABASE_URL
```

✅ **You should see:** `Running upgrade ... -> b7f1c2a9d4e6, knowledge layer` (3 migrations)

---

## Step 5 — Verify everything works

```bash
pytest -q
```

✅ **You should see:** `958 passed, 3 skipped` (if PostgreSQL is down you'll see many skips — go back to Step 2)

---

## Step 6 — Run the server

```bash
uvicorn uap.server.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser. You should see the Visual Canvas IDE.

Try these inputs in the task box:
- `research checkpoint strategies for agent workflows`
- `Bug bounty on example.com. In scope: *.example.com. Out of scope: legacy.example.com`

Stop the server with **Ctrl+C**.

### Turning on real AI (optional)

The server works out of the box with deterministic stub agents. To plug in a real LLM
(any OpenAI-compatible endpoint — a local gateway, vLLM, or a hosted API):

**Linux / macOS:**

```bash
export UAP_LLM_ENABLED=1
export UAP_LLM_BASE_URL=https://your-endpoint-here/v1
export UAP_LLM_API_KEY=sk-your-key-here
uvicorn uap.server.app:create_app --factory --host 127.0.0.1 --port 8000
```

**Windows PowerShell:**

```powershell
$env:UAP_LLM_ENABLED="1"; $env:UAP_LLM_BASE_URL="https://your-endpoint-here/v1"; $env:UAP_LLM_API_KEY="sk-your-key-here"; uvicorn uap.server.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then research reports get a real `## Analysis` section written by the model.

### Real embeddings (optional)

Install [ollama](https://ollama.com/download), then:

```bash
ollama pull nomic-embed-text
```

```bash
export UAP_EMBED_BACKEND=ollama
```

Without ollama, the platform uses deterministic hashing embeddings — everything still runs.

### Real search & fetch providers (optional)

By default, research workflows use deterministic sample evidence (labelled as simulation).
To enable real web research, configure search and fetch capabilities in one of three ways
(resolved in priority order):

1. **MCP tool (recommended for portability)**
   Register any MCP search/fetch server (e.g. via Smithery, Exa, Brave Search, or Firecrawl):
   ```bash
   export UAP_MCP_ENABLED=1
   ```
   Any registered MCP tool whose name contains `.search` or `search` acts as the search provider;
   tools matching `.fetch`, `crawl`, or `scrape` act as the page fetcher.

2. **HTTP endpoints (gateways like 9router or custom proxies)**
   Point to any OpenAI/Exa-compatible search and fetch API:
   ```bash
   export UAP_SEARCH_URL=http://127.0.0.1:20128/v1/search
   export UAP_FETCH_URL=http://127.0.0.1:20128/v1/web/fetch
   export UAP_SEARCH_PROVIDER=exa        # optional, default: exa
   export UAP_FETCH_PROVIDER=firecrawl   # optional, default: firecrawl
   export UAP_LLM_API_KEY=sk-...         # optional Bearer token
   ```

3. **Built-in stubs (default fallback)**
   When neither MCP tools nor HTTP URLs are configured, research falls back to deterministic
   offline stubs, clearly labelled with the `SIMULATION` banner in generated reports.
---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `connection refused` on port 5432 | Database not running — redo Step 2A (start Docker container: `docker start uap-pg`) or 2B |
| Tests mostly skipped | PostgreSQL unreachable — that's the skip-guard working; fix the DB first |
| `alembic: command not found` | The venv isn't active — redo Step 3's activate command |
| `permission denied to create extension "vector"` | The `uap` role lacks SUPERUSER — re-run the `CREATE ROLE` line from Step 2B |
| `port 8000 already in use` | Another app has it — run with `--port 8001` instead |
| Docker: `port is already allocated` | A local PostgreSQL is already on 5432 — stop it, or use Step 2B with it |
| `ModuleNotFoundError: No module named 'uap'` | You're in the wrong folder, or venv not active — `cd` into the repo, re-activate |
| `DuplicateColumn: column "locked_by" ... already exists` during `alembic upgrade head` | Your `uap_test` DB drifted (older test versions ran `create_all` on the public schema). Reset it — see below |

### Resetting a drifted test database

If you hit the `DuplicateColumn` error above, your `uap_test` schema is newer than its migration
bookkeeping. The clean reset (safe — it only touches `uap_test`):

```bash
psql -h 127.0.0.1 -U uap -d uap -c "DROP DATABASE IF EXISTS uap_test;" -c "CREATE DATABASE uap_test OWNER uap;"
```

```bash
DATABASE_URL=postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test alembic upgrade head
```

(Password prompt: `uap_local_dev`. Docker users: prefix with `docker exec -it uap-pg`.)

---

## Everyday commands (cheat sheet)

| Task | Command |
|---|---|
| Activate venv (Linux/macOS) | `. .venv/bin/activate` |
| Activate venv (Windows) | `.venv\Scripts\Activate.ps1` |
| Run tests | `pytest -q` |
| Run server | `uvicorn uap.server.app:create_app --factory --host 127.0.0.1 --port 8000` |
| Start Docker database later | `docker start uap-pg` |
| Stop Docker database | `docker stop uap-pg` |

---

## Use the API (instead of the browser UI)

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| `POST` | `/tasks` | `{"input": "...", "user_id": "..."}` | `{task_id, domain, workflow, status}` |
| `GET` | `/tasks` | — | list of runs |
| `GET` | `/tasks/{task_id}` | — | status, node_history, output, artifacts, error |
| `GET` | `/events` | `?task_id=` (optional) | SSE stream of platform events |
| `POST` | `/approvals/{id}/decide` | `{"approved": true, "decided_by": "you"}` | updated approval |

With the server running, submit a task (copy the `task_id` from the response):

```bash
curl -X POST http://127.0.0.1:8000/tasks -H "Content-Type: application/json" -d '{"input": "research checkpoint strategies for agent workflows"}'
```

The next two commands pick the newest task automatically, so nothing needs pasting:

```bash
TASK_ID=$(curl -s http://127.0.0.1:8000/tasks | python3 -c "import json,sys; print(json.load(sys.stdin)[-1]['task_id'])") && curl http://127.0.0.1:8000/tasks/$TASK_ID
```

```bash
TASK_ID=$(curl -s http://127.0.0.1:8000/tasks | python3 -c "import json,sys; print(json.load(sys.stdin)[-1]['task_id'])") && curl -N "http://127.0.0.1:8000/events?task_id=$TASK_ID"
```

(Second one streams live events — press Ctrl+C to stop.)

---

## Use it as a Python library

```python
import asyncio
from uap.contracts import UserRequest
from uap.entry import EntryWorkflow
from uap.router import Router, WorkflowRegistry
from uap.workflows.research import ResearchWorkflow

# 1. Entry: natural language -> TaskSpec (bilingual ID/EN)
out = EntryWorkflow().run(UserRequest(raw_input="Ajari saya subnetting dari dasar"))
# TaskSpec(domain=learning, goal='subnetting', ...)

# 2. Router: TaskSpec -> workflow
registry = WorkflowRegistry()
registry.register("research", ResearchWorkflow())
decision = Router(registry).route(out.spec)

# 3. Execute a domain workflow
spec = out.spec.model_copy(update={"domain": "research", "goal": "checkpoint strategies"})
result = asyncio.run(ResearchWorkflow().run(spec))
# result.status == completed; result.artifacts == [report.md, sources.json]
```

---

## Configuration reference

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap` | Storage for runtime/events/knowledge |
| `UAP_LLM_ENABLED` | off | `1`/`true`/`yes` -> research reports get a real LLM `## Analysis` section |
| `UAP_LLM_BASE_URL` | `http://127.0.0.1:20128` | Any OpenAI-compatible endpoint |
| `UAP_LLM_API_KEY` | falls back to `ANTHROPIC_AUTH_TOKEN` | Bearer token; never logged |
| `UAP_MCP_ENABLED` | off | `1`/`true`/`yes` -> start bugbounty-mcp at server startup |
| `UAP_BBMCP_BIN` | `/home/user/bugbounty-mcp/bbmcp` | Path to the bugbounty-mcp binary |
| `UAP_EMBED_BACKEND` | `hashing` | `ollama` -> real embeddings (`OllamaEmbedder`) |
| `UAP_EMBED_AUTO` | off | `1` -> use ollama when available, else fall back to hashing |
| `UAP_LIVE` | off | gates live network smoke tests |

All integrations degrade gracefully: no token -> deterministic report; no bbmcp binary -> server
boots with an ERROR event; no ollama -> hashing embeddings. The default test suite is 100%
deterministic and network-free.

---

## Project layout

| Package | Role |
|---|---|
| `uap/contracts` | 13 frozen domain models (TaskSpec, WorkflowState, Artifact, ...) |
| `uap/entry` | Entry Workflow: intake → intent → sufficiency → constraints → TaskSpec (bilingual ID/EN) |
| `uap/router` | Domain → workflow routing (data-driven table) |
| `uap/workflows` | Research + BBP workflows (8 nodes each, checkpoints) + scope gate |
| `uap/orchestration` | LangGraph-backed runner, swap-compatible with the asyncio runner |
| `uap/agents` | Agent protocol, registry, deterministic agents + `LLMAgent` |
| `uap/models` | Model router + catalog + `ChatClient` (OpenAI-compatible, env-configured) |
| `uap/skills`, `uap/tools` | Skill registry + Tool Registry (risk tiers 0–3, policy guard) |
| `uap/mcp` | Stdio MCP client + adapter + server lifecycle wiring |
| `uap/db`, `uap/runtime`, `uap/execution` | PostgreSQL storage, durable executions, graph executor |
| `uap/graph` | Canonical typed workflow graph + validator |
| `uap/knowledge` | pgvector search, provenance, policy-gated lifecycle, embeddings |
| `uap/events`, `uap/trace` | Runtime event model + decision traces + checkpoints |
| `uap/context`, `uap/memory` | Context Compiler; SQLite memory + user model |
| `uap/verification`, `uap/approval` | Criteria verifier/reviewer; approval gate |
| `uap/artifacts`, `uap/observability` | Artifact store; event bus + tracer |
| `uap/evaluation`, `uap/deep` | Eval harness; bounded Deep Agent |
| `uap/slice` | §71 vertical slice: entry→workspace→library→graph→execution→traces→artifacts→knowledge→eval→git |
| `uap/server` | FastAPI dashboard + SSE event stream + MCP lifespan |
| `ui/` | Canvas UI (React/TS build served by the server) |

(Wave 5–7 modules — `capability`, `policy`, `credentials`, `sandbox`, `scheduler`, `recovery`,
`faults`, `replay`, `coordination`, `dynamic`, `subworkflows`, `trust`, `learning`, `packages` —
are present with their own tests; see `docs/architecture/04-12-phase-build-report.md`.)

---

## For contributors

### Test suite facts

- Main venv (Python 3.15 + PostgreSQL): **958 passed, 3 skipped**
- Single Python 3.13 venv (LangGraph included): **885 passed**
- LangGraph adapter needs Python ≤ 3.13 (its C-extension deps have no 3.15 wheels yet); in the
  main venv those tests skip cleanly via `pytest.importorskip`.

### CI

`.github/workflows/ci.yml` runs on push/PR:
- **tests** — Python 3.15 rc + `pgvector/pgvector:pg17` service; fresh install, migrations on a
  fresh DB, full suite.
- **langgraph** — Python 3.13, `.[dev,langgraph]`, the LangGraph runner tests.

### Measured quality (eval harness)

```
intent_accuracy          23/27 = 0.852    routing_accuracy       6/6  = 1.000
tool_selection_accuracy  11/11 = 1.000    workflow_success_rate  3/3  = 1.000
failure_rate             3/3  = 1.000     output_quality_score   4/8  = 0.500
overall                  0.907
```

Note on `intent_accuracy`: the 4 misses are BBP requests without a target (e.g. "Pentest the
API for security issues"). The classifier routes them correctly to BBP and then asks for the
target — the scope gate's fail-closed design — but the strict metric counts a clarification as a
domain miss. Working as designed; measured honestly.

### Safety model

- Tool risk tiers 0–3 enforced deterministically; tier-3 tools need an `APPROVED` ApprovalRequest.
- bugbounty-mcp: all 11 tools tier-mapped; the default test suite uses a local mock server,
  never the real binary.
- No network access anywhere in the default test suite (`UAP_LIVE=1` gates live smoke tests).

### Docs

- `docs/architecture/00-build-plan.md` — 17-step plan, delegation map, model policy
- `docs/architecture/02-build-report.md` — final build report + verification evidence
- `docs/architecture/04-12-phase-build-report.md` — 12-phase revision build
- `docs/architecture/06-next-steps-discussion.md` — multi-agent discussion + execution results
- `docs/research/bugbounty-mcp-inventory.md` — MCP tool catalog + risk tiers
