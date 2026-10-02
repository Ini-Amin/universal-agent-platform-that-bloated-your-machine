# UAP — Design System

> Recorded from the incumbent implementation (`ui/css/app.css`, `ui/js/graph-canvas.js`).
> This documents what exists; it does not invent a new visual world.

## Mode

**Operate.** The visitor completes a task: compose a workflow, run it, watch it execute,
inspect what happened. Scanability, consistency, and density outrank expression. This is closer
to an IDE, a workflow debugger, and an observability dashboard than to a marketing page.

## World

A **dark technical instrument**: GitHub-dark surfaces, hairline borders, monospace for
identifiers, and color used strictly as a signal (status, node kind, risk). No gradients, no
decorative imagery, no oversized empty space. The canvas is the stage; every panel is a tool.

## Color

### Surface ramp

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0d1117` | app background, canvas |
| `--panel` | `#161b22` | panes, node bodies |
| `--panel-2` | `#1c2330` | node header banner |
| `--panel-3` | `#21262d` | raised rows, inputs |
| `--border` | `#30363d` | hairline dividers |
| `--border-light` | `#484f58` | hover / secondary borders |

### Text

| Token | Value | Use |
|---|---|---|
| `--fg` | `#e6edf3` | primary text |
| `--fg-muted` | `#8b949e` | secondary text, metadata, placeholders |

### Signal

| Token | Value | Use |
|---|---|---|
| `--accent` | `#58a6ff` | primary action, selection, running |
| `--accent-muted` | `#1f6feb` | accent fill |
| `--ok` | `#3fb950` | completed, success |
| `--warn` | `#d29922` | paused, degraded, stub evidence |
| `--err` | `#f85149` | failed, destructive |

**Rule:** signal colors carry meaning. Never use `--ok`/`--warn`/`--err` decoratively.

### Node kind palette (canvas)

Each node kind owns a hue, applied as a 3px banner under the node header and as the kind label:

| Kind | Hex | | Kind | Hex |
|---|---|---|---|---|
| input | `#58a6ff` | | subworkflow | `#a371f7` |
| output | `#3fb950` | | synthesis | `#f778ba` |
| agent | `#bc8cff` | | approval | `#e3b341` |
| tool | `#f0883e` | | knowledge | `#79c0ff` |
| condition | `#d29922` | | evaluation | `#db61a2` |
| parallel | `#39c5bb` | | join | `#56d364` |

### Node status (execution overlay)

`running #58a6ff` (pulsing ring) · `completed #3fb950` · `failed #f85149` ·
`paused #d29922` · `pending #8b949e` · `skipped #6e7681`

Status is shown as a ring around the node **and** as a text label — never color alone.

## Typography

| Role | Stack |
|---|---|
| UI | `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif` |
| Identifiers, code, metrics | `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace` |

- Functional text floor: **11px**. Anything interactive or content-bearing below 11px is a
  legibility failure (the detector flags `10px` occurrences — they should be raised).
- Monospace is reserved for things a user might copy or compare: ids, counts, token values,
  model names, ports, event payloads.

## Space and shape

- Radii: **4px** (controls, small), **6px** (panels, nodes), **8–12px** (overlays only).
- Borders: 1px hairlines (`--border`); selection uses 2px `--accent`.
- Panes are separated by borders, not shadows. Elevation is expressed with the surface ramp.
- Density is a feature: metadata grids, compact rows, 11–13px text in panels.

## Components

| Component | Shape | Notes |
|---|---|---|
| `.pane` | bordered region | header + scrollable body |
| `.btn` / `.btn-accent` / `.btn-warn` / `.btn-ok` | 4px radius | disabled state must be visibly disabled |
| `.badge` | pill, 10–11px | status vocabulary; every status value needs a style |
| `.graph-node` | 190px wide SVG group | header (title + kind) + port rows |
| `.list-item` | row | selectable; selection uses `--accent` border/background |
| `.toast` | overlay, 8px radius | non-blocking replacement for `alert()` |
| `.code-block` / `.console-content` | mono, scrollable | artifacts, events, configs |

## Bans (verified anti-patterns to avoid)

1. **No thick one-sided accent borders** (`border-left: 4px solid`) — the most recognizable
   tell of AI-generated UI. Use a subtle full border, a background tint, or a leading dot.
2. **No layout-property animation** (`transition: height/width/padding/margin`) — animate
   `transform`/`opacity`, or use `grid-template-rows` for height.
3. **No functional text under 11px.**
4. **No children flush against a bordered container** — minimum 8px inset (12–16px preferred).
5. **No blocking native dialogs** (`alert`, `confirm`, `window.prompt`) — use the toast system.
6. **No fabricated data in any surface.** Stub output carries the SIMULATION badge; real output
   does not.
7. **No placeholder graph nodes.** If a graph is unknown, show an honest empty state.

## Accessibility floor

- `:focus-visible` ring on every interactive control (2px `--accent`, 2px offset).
- Hit targets ≥ 24×24px for interactive controls.
- Status conveyed by text + shape + color, never color alone.
- Every icon-only control carries an accessible name (`aria-label` or `title`).
- Keyboard: no global shortcut may hijack typing in an input/textarea/contenteditable.
