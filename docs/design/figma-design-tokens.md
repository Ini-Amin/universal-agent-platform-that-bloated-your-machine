# UAP Design Tokens — SDS Dark (Figma AI generation)

Generated 2026-10-03 by Figma AI ("Universal Agent Platform · SDS Dark", 1600×1024).
Full CSS export: 8457 lines captured from Figma AI export (see `paste-2.md`).

## Color Palette

| Token | Hex | Role / Usage |
|---|---|---|
| `--bg` | `#1B1E28` | Main application background, canvas background, editor & terminal buffer |
| `--panel` | `#1E222C` | Sidebar surface, top header bar, stage card surface |
| `--panel-2` | `#212530` | Platform identity bar, card toolbar, secondary panels, tab active state |
| `--panel-3` | `#262B36` | Input field surface, dropdown backgrounds, button neutral fill |
| `--border` | `#2A2F3B` | Primary border line for cards, panes, dividers, inputs |
| `--border-light` | `#2F3542` | Subtle borders, hover borders |
| `--fg` | `#E2E6F0` | Primary text, active icons, code text |
| `--fg-muted` | `#9AA1B4` | Muted secondary text, line numbers, inactive tab labels |
| `--accent` | `#DD7FD3` | Brand/accent pink, active highlights, links, focus rings |
| `--accent-muted` | `#A8499E` | Derived dark pink for filled buttons with white text (WCAG 5.11:1) |
| `--ok` | `#6FC2B8` | Success/teal, status connected, completed run indicator |
| `--warn` | `#D29922` | Warning/amber, paused state, caution badges |
| `--err` | `#F85149` | Error/red, failed status, destructive actions |
| Chip bg | `#2B2039` | Dark purple background for tool chips and badges |
| Icon stroke | `#C8CDDB` | Neutral icon strokes across toolbar and sidebar |

## Fonts

- **UI Font (`--font-sans`)**: `Inter`, system-ui, -apple-system, "Segoe UI", sans-serif
- **Code/Terminal Font (`--font-mono`)**: `Roboto Mono`, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace

Loaded via Google Fonts in `<head>` of `ui/index.html`.

## Border Radii

- **Cards / Containers**: `8px` (code editor, terminal, web preview, modal dialogs)
- **Controls / Inputs**: `6px` (buttons, text inputs, search fields, layout buttons)
- **Badges / Small tags**: `4px` (engine badge, line number gutter tags)
- **Pills / Status dots**: `10px`–`12px` (round counter badges)

## Shadows

- **Card standard**: `0 4px 12px rgba(0, 0, 0, 0.125)`
- **Card focused**: `0 6px 24px rgba(0, 0, 0, 0.45)`
- **Popover / Modal**: `0 16px 40px rgba(0, 0, 0, 0.65)`

## Layout Structure & Landmarks

```
Root: 1600×1000, flex-row, bg #1B1E28
├── Resource sidebar: 240×1000, flex-column, bg #1E222C, border-right #2A2F3B
│   ├── Platform identity: 240×72, flex-row, bg #212530, border-bottom #2A2F3B
│   └── Resources: 240×850, flex-column, gap 16
│       ├── Navigation: Canvas, Runs
│       ├── Library: Agents, Tools, Skills, Knowledge, Artifacts
│       └── System: Models, MCP, Integrations, Policies
└── Operator workspace: 1360×1000, flex-column
    ├── Workspace toolbar: 1360×72, bg #1E222C, border-bottom #2A2F3B
    │   ├── Workspace picker & New Workspace button
    │   ├── Task input & run controls
    │   └── Stream & task status badges
    └── Infinite canvas: 1360×928, bg #1B1E28
        ├── Canvas actions: Zoom (+/-), Fit, Reset, Layout (Free, Split 2, Split 4), + Tools
        ├── Code editor card: 8px radius, bg #1B1E28, border #2A2F3B, shadow 0 4px 12px
        ├── Terminal card: 8px radius, bg #1B1E28, border #2A2F3B, shadow 0 4px 12px
        └── Web preview card: 8px radius, bg #1B1E28, border #2A2F3B, shadow 0 4px 12px
```

## Key Dimensions

| Component | Width | Height | Radius |
|---|---|---|---|
| Root Viewport | 1600 | 1000 | — |
| Resource Sidebar | 240 | stretch | — |
| Top Header / Toolbar | 100% | 72 | — |
| Stage Card (Editor) | 640 | 480 | 8 |
| Stage Card (Terminal) | 560 | 480 | 8 |
| Tool Palette Popover | 560 | auto | 8 |
| Action Buttons | auto | 32–34 | 6 |
