# Figma content map — what to implement, what never to ship

Companion to [`figma-design-tokens.md`](./figma-design-tokens.md).

The design frame `Universal Agent Platform · SDS Dark` is a **visual reference**,
not a data source. It was authored with realistic-looking filler so the layout
reads well: a fake user (`Alex Kim`), a fake workspace (`Usage dashboard`), a
fake run (`Run complete · 00:42`), and a whole fabricated analytics product
(`acme / Analytics`, `128,430 API requests`). None of it describes UAP.

The previous pass copied that filler into the app as if it were real state. This
document exists so that mistake is not repeatable: every landmark in the frame is
classified below as either **STRUCTURE** (implement it — it is chrome, layout or
a control) or **DEMO CONTENT** (never ship it — replace with real state or an
honest empty state).

## The rule

> **Do not copy demo content from a design file.**
> A design file shows *shape and hierarchy*. Any literal text in it is a
> placeholder unless it is a control label (`History`, `Share`, `Add surface`),
> a product name (`Universal`, `AGENT PLATFORM`), or a generic UI noun
> (`Agents`, `Tools`, `Search resources`). Names of people, workspaces, runs,
> metrics, files, endpoints, users, and code are **DEMO CONTENT** and MUST be
> replaced with real state or an honest empty state.

Concrete failures this rule prevents (all were shipped, all are now removed):

| Shipped as real state | Why it was wrong | Now |
|---|---|---|
| `Alex Kim` / `Pro workspace` / `AK` avatar | A person who does not exist | `Operator` / `Local workspace` / `OP` (see `identity_source` below) |
| `Build a customer usage dashboard` prefilled in the task input | The mock's example task | empty input, real placeholder |
| `Usage dashboard` workspace title, `Development` env badge | Mock workspace + mock environment | real workspace name from `GET /api/workspaces`; environment from hostname |
| `Run complete` + `00:42` before any run | A run that never happened | `idle` + `No run`, driven by `executionStore` |
| `DASHBOARD_SRCDOC` — a whole `acme / Analytics` dashboard (`128,430` API requests, `2,048` active users, pink spline chart) rendered in the web-preview surface | A product that does not exist, presented as UAP's own data | deleted; the preview surface shows an honest "Nothing loaded" empty state |
| `App.tsx` / `MetricCard` / `UsageChart` TSX sample in the editor | The mock's demo file | deleted; the editor uses the real `DEFAULT_PYTHON` template |
| Sidebar rows `Builder`, `Researcher`, `Reviewer`, `Product brief`, `Design guidelines`, `usage-dashboard`, `test-results.json`, `Code interpreter`, `File system`, `Database`, `API integration` | Invented inventory | populated from the real API (`/api/resources/*`, `/api/knowledge`, `/api/tasks/{id}/artifacts`); empty sections say so |
| Terminal `8 tests passed (8)`, `Scaffolded React + TypeScript project`, `Preview ready at http://localhost:5173`, `Exit code 0 · 42s · 6 tool calls` | A build log from the mock | terminal starts empty; the run summary only renders when a run reports one |
| Handoff card `8 / 8 tests passed`, `Dashboard built and verified…`, `Builder` | Mock agent output | neutral `Active` / `No summary provided.` / `Agent` |

## Landmark map

Source of the landmark list: the Figma export
(`paste-5.md`, 8457 lines of CSS) and the authoritative file
(`luXdGtltH2zNc6IYZE7JOs`, fetched via the Figma REST API — see
"Figma API" below). Paths are `Frame > Frame > … > Node`.

### Sidebar

| Landmark path | UAP implementation | Class |
|---|---|---|
| `Resource sidebar` | `.pane-sidebar` (`240px`, `--panel`) | STRUCTURE |
| `Platform identity > Platform name > Label` (`Universal` / `AGENT PLATFORM`) | `.sidebar-identity` product name | STRUCTURE (product name) |
| `Platform identity > Collapse sidebar` | `#btn-collapse-sidebar` | STRUCTURE (control) |
| `Resources > Resource search > Value` (`Search resources ⌘ K`) | `.resource-search-input` placeholder | STRUCTURE (control label) |
| `Resource section > Section toggle > Label` (`Agents`/`Tools`/`Skills`/`Knowledge`/`Artifacts`) | `.sidebar-section-title` | STRUCTURE (generic UI noun) |
| `Resource section > Section toggle > Label` (`3`/`6`/`2`) | `.sidebar-section-count`, **computed from the API** | DEMO CONTENT (hardcoded count) |
| `Resource section > Resource > Resource name` (`Builder`, `Code interpreter`, `Product brief`, `usage-dashboard`, …) | `data-items-for` slots filled from `/api/resources/*`, `/api/knowledge`, `/api/tasks/{id}/artifacts` | DEMO CONTENT |
| `Resource section > Resource > Label` (`Idle`, `Web`) | per-item badge/status from real data | DEMO CONTENT when invented |
| `Runtime environment > Environment status > Status > Label` (`Sandbox connected`) | `.env-status-row .status-label` | STRUCTURE (generic state label) |
| `Account > Avatar > Initials` (`AK`) | `.account-avatar` → `OP` | DEMO CONTENT (fake person) |
| `Account > Account details > Label` (`Alex Kim`, `Pro workspace`) | `.account-name` / `.account-role` → `Operator` / `Local workspace` | DEMO CONTENT (fake person) |

### Toolbar

| Landmark path | UAP implementation | Class |
|---|---|---|
| `Operator workspace > Workspace toolbar` | `.workspace-toolbar` (`72px`) | STRUCTURE |
| `Workspace selector > Label` (`UAP`) | `#workspace-select` default option | STRUCTURE |
| `Workspace navigation > Label` (`Usage dashboard`) | `.workspace-title-label`, set from the selected workspace's real `name` | DEMO CONTENT (mock workspace) |
| `Environment badge > Label` (`Development`) | `#env-badge`, `Local`/`Remote` from hostname | DEMO CONTENT (mock environment) |
| `Run controls > Status > Label` (`Run complete`) | `#active-status-badge`, from `executionStore.status` (`idle` at rest) | DEMO CONTENT (fake run) |
| `Run controls > Label` (`00:42`) | `#active-task-label` (`No run` at rest) | DEMO CONTENT (fake elapsed time) |
| `Run controls > History > Button` (`History`) | `#btn-history` | STRUCTURE (control) |
| `Run controls > Action > Label` (`Run again`) | `#btn-run-again` | STRUCTURE (control) |
| `Canvas actions > Add surface > Button` (`Add surface`) | `#btn-tools-palette` | STRUCTURE (control) |
| `Canvas actions > Share > Button` (`Share`) | `#btn-share-canvas` | STRUCTURE (control) |

### Canvas

| Landmark path | UAP implementation | Class |
|---|---|---|
| `Infinite canvas` | `#canvas` stage host | STRUCTURE |
| `Task context > Task description > Task breadcrumb > Label` (`WORKSPACE`, `BUILD SESSION`) | `.task-context-breadcrumb` | STRUCTURE (generic nouns) |
| `Task context > Task description > Task title` (`Build a customer usage dashboard`) | `#task-input` — **empty**, real placeholder | DEMO CONTENT (mock task) |
| `Task context > Task description > Label` (`Builder agent · React + TypeScript · Last run just now`) | `#task-context-meta` (`Ready to run` at rest) | DEMO CONTENT (fake run metadata) |
| `Left column > Code editor` | editor card (real `ui/js/editor.js`) | STRUCTURE |
| `Editor tabs > File tab > Label` (`App.tsx`, `styles.css`, `package.json`) | tabs from real opened files | DEMO CONTENT |
| `Source code > Code line > Source` (`MetricCard`, `128,430`, …) | editor buffer from `DEFAULT_PYTHON` or a real file | DEMO CONTENT |
| `Editor status > Branch status > Label` (`main*`, `0 problems`) | git status from the real workspace | DEMO CONTENT when invented |
| `Terminal` | terminal card (`ui/js/stage.js`) | STRUCTURE |
| `Terminal tabs > Active terminal tab > Label` (`Run output`, `Shell`, `Problems`) | `.stage-terminal-tab` | STRUCTURE (generic tab names) |
| `Run log > Log entry` (`14:32:08 builder Scaffolded…`, `8 tests passed (8)`) | terminal buffer; **empty** until a real run streams | DEMO CONTENT |
| `Run summary > Status > Label` (`Process exited successfully`, `Exit code 0 · 42s · 6 tool calls`) | terminal summary, rendered only when a run reports one | DEMO CONTENT |
| `Right column > Web preview` | iframe card | STRUCTURE |
| `Browser controls > Preview address > Label` (`localhost:5173`) | address bar showing the real `spec.url` | DEMO CONTENT (mock URL) |
| `Rendered dashboard > Application navigation > Label` (`acme`, `Analytics`) | — | DEMO CONTENT |
| `Rendered dashboard > Overview heading > Overview copy > Title` (`Usage overview`) | — | DEMO CONTENT |
| `Rendered dashboard > Key metrics > Metric` (`API requests 128,430`, `Active users 2,048`) | — | DEMO CONTENT |
| `Rendered dashboard > Request volume` (`8k`/`16k`/`24k`, `Oct 3 … Sep 27`) | — | DEMO CONTENT |
| `Rendered dashboard > Endpoint breakdown` (`/v1/completions 84,216`, `/v1/embeddings 32,480`) | — | DEMO CONTENT |
| `Right column > Agent handoff > Agent summary` (`Dashboard built and verified…`) | `spec.summary`, else `No summary provided.` | DEMO CONTENT |
| `Completion heading > Status > Label` (`8 / 8 tests passed`) | `spec.statusText`, else `Active` | DEMO CONTENT |
| `Completion heading > Agent identity > Label` (`just now`, `Builder`) | `spec.agent`, else `Agent` | DEMO CONTENT (fake agent name) |
| `Agent handoff > Follow-up prompt > Prompt hint` (`Ask Builder to make a change…`) | `Ask agent to make a change...` | STRUCTURE (generic control hint) |
| `Infinite canvas > Session metadata > Label` (`session_8f2a`, `All changes saved`) | session id / save state from real state | DEMO CONTENT |
| `Canvas hint` (`Space + drag to pan · Scroll to zoom`) | canvas help text | STRUCTURE (interaction help) |

## Identity source

There is **no** authenticated-user endpoint. `src/uap/server/auth.py` resolves an
`Identity` server-side (roles `admin` / `member` / `viewer`), and
`GET /api/workspaces` filters by `identity.user_id`, but nothing exposes the
current principal to the client. In local single-user mode auth is off and no
identity exists at all.

Per the constraint "do not edit `src/uap/server/*.py`", the account pill is
therefore a **neutral placeholder** — `Operator` / `Local workspace` / `OP` —
which matches the platform's own vocabulary for its single-operator mode
(`docs/architecture/13-night-session-handover.md`: "Multi-user is still
single-operator… accounts, roles, and shared workspaces do not [exist]").

To show a real name later, a backend route (e.g. `GET /api/auth/me`) must be
added; that is out of scope for this change.

## Figma API

The Figma REST API **was used** and still authenticates:

```
GET https://api.figma.com/v1/files/luXdGtltH2zNc6IYZE7JOs
X-Figma-Token: <FIGMA_API_KEY from environment>
→ HTTP 200, document name "Heh", 2039 nodes, 667 TEXT nodes
```

The fetched node tree confirmed, authoritatively, that every value the previous
pass shipped is a **mock placeholder** — the frame literally names its nodes
`Resource name`, `Metric value`, `Preview address`, etc., and the values are
`Alex Kim`, `128,430`, `localhost:5173`. The landmark paths in the tables above
come from that tree, so future work can map a Figma node to a UAP landmark
without guessing. The token was only read from the environment; it was not
written to any file.
