# UAP Design Tokens — from Figma AI generation

Generated 2026-10-03 by Figma AI (using owner's AI credits via browser relay).
File: `luXdGtltH2zNc6IYZE7JOs` (frame "Universal Agent Platform", 1600×1000).
Full CSS export: 8715 lines captured to `figma-export.css` (see session paste).

## Color palette (by frequency)
```
#64748B  muted gray (icons, secondary text)     x83
#94A1B5  mid gray (icon strokes)                x71
#E8EDF5  primary text                           x37
#7A879A  dim text                               x22
#283242  border line                            x19
#79C6A1  success/run green                      x13
#4F8CFF  accent blue (matches existing UAP)     x11
#1C293C  panel surface                          x10
#141B25  toolbar surface                        x10
#FFFFFF  pure white                             x9
#0D1117  app background (matches existing UAP)  x9
#E4E9F1  light text                             x8
#10151D  sidebar surface                        x5
#B8A4DD  purple accent                          x3
#1A2330  card surface                           x3
#1B2D4D  brand mark / active section            x2
#39816B  dark green (run button text)           x2
```

## Fonts
- `Inter` — all UI text (107 uses)
- `JetBrains Mono` — code/terminal (59 uses)

## Layout structure (top-level)
```
root: 1600×1000, flex-row, bg #0D1117
├── Resource sidebar: 240×1000, flex-column, bg #10151D, border-right #283242
│   ├── Platform identity: 240×72, flex-row, border-bottom #283242
│   │   ├── Brand mark: 30×30, bg #1B2D4D, border #4F8CFF, radius 8
│   │   ├── Platform name: 82×30, flex-column, gap 2
│   │   └── Sidebar control: 68×16 (panel-left-close icon)
│   └── Resources: 240×850, flex-column, padding 20 12 0, gap 16
│       ├── Resource search: 216×34, bg #0D1117, border #283242, radius 6
│       ├── Resource section (Agents): 216×151, bg #1B2D4D, radius 6
│       │   section toggle (chevron + bot icon + label + count + plus)
│       │   resource rows: bot icon + name + running indicator
│       ├── Resource section (Tools): wrench icon
│       ├── Resource section (Skills): chevron-right + sparkles
│       ├── Resource section (Knowledge): book-open
│       ├── Resource section (Artifacts): box icon
│       └── Resource section (MCP / Integrations)
└── Operator workspace: 1360×1000, flex-column
    ├── Workspace toolbar: 1360×72, bg #10151D
    │   ├── Workspace navigation
    │   ├── Workspace selector: 74×16, bg #283242, radius 6
    │   └── Run controls: 419×34, bg #79C6A1, radius 8
    └── Infinite canvas: 1360×928
        ├── Canvas context and controls: 1360×928 (overlay)
        ├── Canvas hint
        ├── Canvas navigation
        ├── Canvas breadcrumb: 162×12
        ├── Canvas actions: 203×34, bg #141B25, radius 8
        ├── Canvas grid
        ├── Code editor: 650×426, bg #0D1117, radius 12
        │   ├── Surface toolbar: tabs
        │   ├── Editor tabs: 650×34, bg #10151D
        │   └── Editor status
        ├── Terminal: 650×272, bg #0D1117, radius 12
        │   ├── Surface toolbar
        │   └── Terminal tabs (active tab)
        └── Web preview: 614×718, bg #0D1117, radius 12
```

## Key dimensions
| Component | Width | Height | Radius |
|---|---|---|---|
| Root | 1600 | 1000 | — |
| Sidebar | 240 | 1000 | — |
| Sidebar identity bar | 240 | 72 | — |
| Workspace toolbar | 1360 | 72 | — |
| Canvas | 1360 | 928 | — |
| Code editor card | 650 | 426 | 12 |
| Terminal card | 650 | 272 | 12 |
| Web preview card | 614 | 718 | 12 |
| Resource section | 216 | 151 | 6 |
| Resource search | 216 | 34 | 6 |
| Brand mark | 30 | 30 | 8 |
| Canvas actions | 203 | 34 | 8 |
| Run button | 419 | 34 | 8 |

## Notes
- The design confirms UAP's existing palette (#0D1117 bg, #4F8CFF accent).
- Adds: #10151D sidebar surface, #283242 borders, #1B2D4D active section,
  #79C6A1 success green, #141B25 toolbar, #64748B/#94A1B5 icon grays.
- Card radius is 12px (UAP currently uses 10px — minor bump).
- Inter font everywhere, JetBrains Mono for code.
- The sidebar has collapsible resource sections with chevron toggles,
  resource counts, and add (+) buttons — richer than UAP's current sidebar.
