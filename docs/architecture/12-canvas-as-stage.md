# Target Architecture — Canvas as the Stage

Date: 2026-10-03 · Status: **decided with the owner**, building
Reference: https://www.themaestri.app/en · Templates: ComfyUI model

---

## What was wrong

The owner's verdict, after using the product:

> "I want output & artifacts but the result is not visible."
> "Each node, when opened, should show the agent's process."
> "If it's only like this, it feels like it has no impact."

He is right, and the reason is architectural. UAP was built as an
**observability dashboard**: it shows that a workflow ran (nodes turn green,
events stream, traces record). It never shows **what the work produced**.

The canvas could render exactly one thing: an SVG node graph. No video, no
image, no editor, no browser, no whiteboard. A node's entire visual vocabulary
was a title and a colour.

So the product answered "did it run?" and never "what did it make?" — which is
the only question a user has.

---

## The decision

**UAP is a shared workspace where a user and agents work on one canvas.**

Not a monitoring panel. The canvas is the stage; the workflow graph becomes
navigation.

| | Before | After |
|---|---|---|
| Centre | SVG node graph | **Stage** — real tool views |
| Graph | the main event | **side strip** (~80px), compact chips |
| Fullscreen | not possible | stage expands; hovering the left edge reveals `>` to bring the strip back |
| A node | title + colour | **a view**: video, markdown, code, image, browser, whiteboard |
| First use | blank page | **template browser** (ComfyUI-style) |
| Autonomy | fixed | **user's choice** (recon-only → full) |

---

## Verified reference behaviour

**Maestri** (themaestri.app) — fetched and read:
- Infinite canvas; each agent is a node the user drags around and connects.
- **Portals**: embedded windows on the canvas — a website, an iOS simulator, an
  Android emulator, or a real device. An agent connected to a portal clicks and
  types by reading the real element tree.
- **Notes** that agents write to directly; **drawing** for architecture sketches.
- **Partituras**: save a corner of the canvas (agents + roles + notes + portals
  + drawings + connections) and drop it back anywhere, fully assembled.
- Stated principle: *"the canvas holds the state of the project"* and
  *"work moves between them while you watch"*.
- Pricing: free tier, $18 one-time Pro. Fully local, no telemetry, no account.

**ComfyUI templates** — fetched from the real index (1000 entries):
```json
{ "moduleName": "default", "category": "Foundation", "title": "Image",
  "icon": "icon-[lucide--image]", "type": "image",
  "templates": [ { "name": "image_z_image_turbo", "title": "...",
                   "description": "...", "tags": [...],
                   "mediaType": "webp", "tutorialUrl": "..." } ] }
```
That shape is proven at scale, so UAP adopts it rather than inventing one.

---

## What the owner asked for, concretely

1. **Learning**: the agent calls an IDE/editor, puts a video player on the canvas
   with a transcript, explanation, summary, and sources.
2. **Whiteboard**: the agent can call Excalidraw and draw on the canvas.
3. **Bug bounty**: automated against HackerOne / Immunefi / YesWeHack — the
   platform lists the programs, the user should not have to hand it a target.
4. **Per-node process**: opening a node shows the agent opening a browser,
   testing, running a script or Burp Suite.
5. **Participation**: watch the agent work live, and be able to join in.
6. **Autonomy**: full or semi, chosen by the user.

---

## Provider reality (checked, not assumed)

| Platform | Endpoint | Result | Meaning |
|---|---|---|---|
| HackerOne | `api.hackerone.com/v1/hackers/programs` | **401** | needs the user's API credentials |
| YesWeHack | `api.yeswehack.com/programs` | **200** | program list is reachable |
| Immunefi | `immunefi.com/bug-bounty/` | **200** | page reachable; API path redirects (308) |

So automatic program discovery is possible for at least two of the three, but
HackerOne needs the user to connect an account. Submission is a separate
question: it is the user's account, the user's legal exposure, and the
platform's ToS — which is exactly why autonomy is a **setting**, not a fixed
behaviour.

---

## Build order (agreed)

**Foundation first**, because one piece of work unlocks every domain:

1. **Stage** — the canvas hosts arbitrary views (`ui/js/stage.js`).
2. **Node views** — a node can publish what it produced; persisted, survives reload.
3. **Templates** — the catalog, so the first screen is a choice, not a void.

Then the domains become plugins on that foundation:

4. Learning: video + transcript + notes + sources + whiteboard.
5. Bug bounty: provider integration, real testing, evidence, report.
6. Participation: live view of the agent's browser/terminal, plus take-over.
7. Partituras equivalent: save/restore a workspace setup.

The existing strengths — durable execution, the scope gate that refuses
out-of-scope targets before any tool runs, the event/trace store, per-domain
model routing — stay. They are the part that was already right.
