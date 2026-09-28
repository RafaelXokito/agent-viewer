# Feature: live relation graph canvas

Status: final, 2026-09-28; the user's answers to the open questions of draft 1 are applied throughout (section 15).
Scope: requirements only; nothing in this document is implemented yet.
This document is the source of truth for the feature; where it and `SPEC.md` differ, this document wins until GA folds it into `SPEC.md`.
Base: `SPEC.md` draft 1, the code under `agent_viewer/`, and the contract under `contract/examples/`.

## 1. Request and interpretation

The user asked for "a feature to show the live relation graph of the session in a canvas-style perspective", with a toggle to enable it, a card per session that can be opened to see its content, and visible relations between sessions and the tools and skills they called.

Concretely this document specifies:
- a toggle in the session view between the existing tree layout and a new graph canvas layout, remembered in the URL;
- an infinite canvas with pan and zoom (Figma, Miro, tldraw style) whose nodes are cards for the agents of a session (main and nested subagents), cards for the sessions it continues from or into, and optional tool and skill nodes;
- edges for spawn (parent to child agent), resume (`SendMessage`), continued-in between sessions, and agent to tool or skill usage with counts;
- live updates through the existing SSE stream, without a reload;
- opening a card shows that agent's content by reusing the existing timeline and stats views in a side drawer;
- opening a tool node shows the owner agent's timeline filtered to that tool's calls, through a new `toolName` filter on the existing `/events` endpoint;
- a toggleable minimap in a corner of the canvas.

## 2. Key decisions

| Topic | Decision | Why |
|---|---|---|
| Scope for v1 | One session per canvas: all its agents, plus its continuation chain (up to 5 sessions each way) as collapsed session cards | See 2.1 |
| Layout | Deterministic top-down tidy tree over spawn edges, hand-rolled, with leaf wrapping; resume, continuation and tool edges drawn over it without affecting it | See 2.2 |
| Rendering | HTML cards absolutely positioned in one transformed "world" element, plus one SVG layer underneath for edges | See 2.3 |
| Libraries | None vendored | See 2.3 |
| Tool and skill nodes | Aggregated per agent (one node per distinct tool name), top 5 shown per agent plus a "+N more" node; default mode shows them for every visible agent | See 2.4 |
| Backend | New endpoint `GET /api/sessions/{source}/{sessionId}/graph` built by a new pure module `agent_viewer/graph.py` | See 2.5 |
| SSE | New opt-in event `graph.update` carrying the full graph snapshot, coalesced and limited to 1 per session per second | See 2.5 |
| Tool filter | New `toolName` query parameter on the existing `GET /api/agents/.../events`; a tool node opens the drawer timeline filtered to that tool, with a clearable chip; the filter is in the URL | See 2.6 |
| Minimap | In v1: a corner overview of the whole graph with a viewport rectangle, click or drag to pan, toggleable, synced on live updates and zoom | See 2.7 |
| URL state | Layout, tools mode, drawer state and tool filter all live in the hash query only; nothing is kept in `localStorage` | See G-FR-2 |

### 2.1 Scope: single session plus continuation chain

Recommended for v1: the canvas shows one session.
Its agents are fully expanded; the sessions it continues from and into are shown as session cards linked by `continued_in` edges, and clicking one navigates to that session's canvas.

Reasons:
- The unit of the existing model is the session: the tree (SPEC 6), the SSE subscription (`session=<key>`, SPEC 7.4) and the URL (`#/s/<source>/<sessionId>`) are all per session, so a single-session graph reuses all of them.
- Real data is small per session and large across sessions.
  On this machine the largest session has 89 agents (depth 2, one node with 30 children, 1,893 tool calls over 15 distinct tools), while a project-wide graph would join 83 sessions of which only 2 are linked by continuation.
  Most sessions would be disconnected islands, which a relation graph does not help with; the session list already covers that.
- A multi-session graph needs a different subscription model (list-level SSE only carries summaries, not trees), a forest layout, and a new aggregation endpoint over sessions; none of that is needed to answer "what did this session spawn and call".

A project-wide multi-session canvas is a non-goal for v1 (section 4) and is planned for a later version (section 15).

### 2.2 Layout: deterministic tidy tree, not force-directed

Spawn edges form a tree by construction: every agent has exactly one `parentKey` after cycle breaking (SPEC 6.1 step 8), and orphans hang from the root.
A tree layout is therefore exact, not an approximation.

| Criterion | Tidy tree (top-down) | Force-directed |
|---|---|---|
| Determinism | Same input gives the same coordinates, testable with `node --test` | Depends on seed and iteration count |
| Live updates | A new child moves only its later siblings and ancestors' right-hand neighbours | Every node jitters on every change |
| Overlap | None by construction | Needs collision forces, still overlaps with 240px cards |
| Cost | O(n) | O(n^2) per iteration without a quadtree |
| Reading order | Matches the tree view and spawn order | None |

Force-directed layout is rejected.
Non-tree edges (resume, continued-in, tool usage) are drawn on top of the tree layout and never move nodes.

### 2.3 Rendering technology and libraries

Recommended: HTML cards in a single "world" `div` whose `transform: translate(x, y) scale(k)` is set through the CSSOM, plus one `svg` element inside the world for edges.

- HTML cards give native focus, `role` and `aria-*` attributes, text wrapping and ellipsis, and keep the SPEC 11 rule that transcript text only enters the page through `textContent`, using the existing `dom.js` builder.
- Pan and zoom change one CSS transform; no card is re-laid out while panning.
- The CSP allows this: `style-src 'self'` blocks `style="..."` attributes in markup and `setAttribute('style')`, but not CSSOM writes such as `el.style.setProperty()`, which `dom.js` `setVar` already uses.
- SVG edges are `path` elements with `d` set through `setAttribute`; no `href`, no `foreignObject`, no SVG `use` of external resources.
- Canvas 2D was considered and rejected: it needs its own hit testing, text layout and an accessibility shadow tree, and it gives no benefit at the node counts of section 9.

Vendoring was evaluated and rejected for v1:
- dagre (MIT) solves general layered DAG layout, which is not needed for a tree; its maintained builds are about 280 KB with graphlib.
- elkjs (EPL-2.0) is about 1.5 MB, runs best in a worker, and EPL-2.0 is a weaker fit than MIT or ISC for a small local tool.
- cytoscape.js (MIT) renders to canvas and brings its own event and style system, conflicting with the `dom.js` safety model.
- d3-hierarchy (ISC) has a correct Reingold-Tilford tidy tree in about 12 KB and would be acceptable to vendor as a single local file if the hand-rolled layout proves inadequate; this is the documented fallback, not the plan.

The hand-rolled layout is about 150 lines of pure JavaScript, fully unit-tested, with no supply chain.

### 2.4 Tool and skill aggregation

A session with 10,000 tool calls has at most a few dozen distinct tool names per agent (observed maximum: 15 over the whole 89-agent session).
One tool node per (agent, tool name) bounds node count by agents times distinct tools, not by calls.

- The server returns up to `TOOLS_PER_AGENT = 50` tool nodes per agent, ordered by calls descending, then name; the rest are summarized on the agent node (`toolsOmitted`, `toolsOmittedCalls`).
- The client shows the top `TOOLS_VISIBLE = 5` per agent and synthesizes one "+N more" node for the remainder.
- Tool calls that are already drawn as relations are not drawn as tool nodes: `Agent`, `Task`, `task` (spawn edges), `SendMessage` (resume edges) and `Skill` (skill nodes).
  They still count in the agent's `toolCalls` and are reported as `relationToolCalls`.
- Tools mode is a canvas control with three values: `none` (counts only, on the card), `selected` (tool and skill nodes for the selected agent only), `all` (for every visible agent, the default; above the node guard it falls back to `selected`).
- `all` has a guard: if it would render more than `MAX_VISIBLE_NODES = 1500` nodes, the view stays in `selected` and says why.

### 2.5 Backend: new endpoint and new SSE event

The current API is not enough for the initial load.
The tree (`/tree`) has agents, spawn parents, resumes and statuses, but per-agent tool and skill counts are only available from `/api/agents/.../stats?scope=agent`, which means one request per agent (89 requests for the largest real session) and a `409 not_ready` while the session is indexing.
The continuation chain beyond one hop needs one `/api/sessions/...` request per hop.

Decision: add `GET /api/sessions/{source}/{sessionId}/graph`, built server-side by a pure function in `agent_viewer/graph.py` from data the store already holds (tree dict, per-agent stats dicts, pending tool calls, session summaries).

For live updates, deriving the graph on the client from `tree.update` plus per-agent `stats.update` plus `session.upsert` would work with no backend SSE change, but it would duplicate the edge and aggregation rules in Python and JavaScript.
Decision: one builder, on the server, and a new SSE event `graph.update` carrying the full snapshot:
- opt-in with `graph=1` on `/api/stream`, so tree-layout tabs never receive it;
- coalesced per session for 250 ms like other events and limited to one per session per `GRAPH_MIN_INTERVAL = 1.0 s`;
- published only when the built graph differs from the previous one, with a monotonic `rev`;
- when the serialized graph exceeds `MAX_INLINE_GRAPH_BYTES = 512 KiB`, the event carries `graph: null, overflow: true` and the client refetches `/graph`, mirroring the `events.append` overflow rule in SPEC 7.4.

The existing events keep flowing unchanged; the drawer's timeline and stats still use `events.append`, `agent.reset` and `stats.update`.

### 2.6 Tool filter on `/events`

Clicking a tool node must show only that tool's calls, and the timeline pages its events from the server, so filtering on the client would leave pages sparse or empty.
Decision: add an optional `toolName` parameter to `GET /api/agents/{source}/{sessionId}/{agentId}/events`, applied on the server before paging, combined with `kinds` and `includeMeta` by AND.
It matches events whose `toolName` equals the value exactly (case-sensitive), which covers both `tool_call` and `tool_result` events because both carry `toolName` (verified on the fixtures), so call and result stay paired in the timeline.
The drawer shows a chip "filtered: <tool>" with a clear button, and the URL carries the filter as `tool=<name>` so a shared link opens the same filtered view.

### 2.7 Minimap

A minimap is in v1, owned by GB.
It is a small overview in the bottom-right corner of the canvas that draws every visible node of the current layout as a rectangle in its status or kind color, plus a rectangle for the current viewport.
It reuses the layout output (positions and bounds), so it costs no extra layout and cannot disagree with the canvas.
It is drawn as one SVG with one `rect` per node (no text, so no transcript content), toggleable from the toolbar, shown by default on screens wider than 720 px and hidden by default below.

## 3. Goals

- GG1 Show one session's agents and their relations as a spatial graph that can be panned and zoomed.
- GG2 Make every relation the model already knows visible: spawn, resume, continuation, tool usage and skill usage, with counts.
- GG3 Update live: new agents, status changes, new tool and skill usage appear within 2 s of the transcript growing, without a reload and without losing the user's viewport.
- GG4 Open any agent's content from its card by reusing the existing timeline and stats views.
- GG5 Remain safe and dependency-free: no build step, no npm, no CDN, CSP unchanged, transcript text only through `textContent`.
- GG6 Remain usable with keyboard only and with the existing tree layout always one click away.

## 4. Non-goals (v1)

- A project-wide or multi-session canvas beyond the continuation chain (planned for a later version).
- Editing the graph: no dragging cards to new positions, no manual layout persistence.
- Force-directed or user-selectable layout algorithms.
- Per-call tool nodes (one node per tool call) or a timeline scrubber that replays the graph over time.
- Shared tool nodes (one `Bash` node per session with edges from every agent).
- Edges for Oh My Pi `irc:incoming` messages and for Claude `spawnSeenIn` cross-session spawns (planned for a later version).
- Remembering the layout or tools mode across visits in `localStorage`; the URL is the only store of view state.
- Drawing spawn and relation tools (`Agent`, `Task`, `task`, `SendMessage`, `Skill`) as tool nodes; they are shown only as edges and skill nodes.
- Applying the tool filter or drawer state in the tree layout; they are graph-layout state and are dropped when switching to the tree layout.
- Export to image or file.
- Any change to parsers, tree reconstruction rules, or status rules.

## 5. User stories

- US-1 As a developer watching a running session, I switch the session view to the graph and see main, its subagents and their nested subagents as cards laid out top-down, so I understand the delegation structure at a glance.
- US-2 As that developer, I see a new subagent card appear under its parent within 2 s of it being spawned, and its status dot change as it runs and finishes, without my viewport jumping.
- US-3 As a reviewer, I see which agent resumed which (dashed arrows with counts), so I can follow coordinator messages.
- US-4 As a reviewer, I select an agent and see the tools and skills it used as nodes with call and error counts, so I can spot the agent that ran 1,300 `Bash` calls or failed repeatedly.
- US-5 As a reviewer, I click or press Enter on a card and read that agent's full timeline and stats in a drawer, without leaving the canvas.
- US-6 As a user of a continued session, I see "continued from" and "continued in" session cards next to the root and can jump to them.
- US-7 As a keyboard user, I move between cards with the arrow keys, open one with Enter, close the drawer with Escape, and zoom with `+`, `-` and `0`.
- US-8 As a user who shares a link, I copy the URL and the recipient opens the same session in the graph layout with the same agent selected.
- US-9 As a user of a very large session, the canvas stays responsive and, if it cannot show everything, tells me what it left out and offers the tree view.
- US-10 As a reviewer, I click the `Bash` node of an agent and the drawer shows only that agent's `Bash` calls and results, with a "filtered: Bash" chip I can clear; I can share the URL of that filtered view.
- US-11 As a user of a large session, I see a minimap of the whole graph with my viewport outlined, and I click or drag in it to jump to another part of the graph.

## 6. Functional requirements

### 6.1 Toggle and routing

- G-FR-1 The session view header shows a two-option layout control, "Tree" and "Graph", implemented as two buttons with `aria-pressed`; "Tree" is the default.
- G-FR-2 The layout is stored in the hash query: `#/s/<source>/<sessionId>?layout=graph` and `#/s/<source>/<sessionId>/a/<agentId>?layout=graph`; `layout=tree` or no `layout` means the tree layout, and any other value is treated as `tree`; the layout is not stored anywhere else.
- G-FR-3 The tools mode is stored in the same query as `tools=none|selected|all`, omitted when it is the default `all`; an invalid value is treated as `all`.
- G-FR-3a The drawer state is stored in the same query as `drawer=timeline|stats` (open, on that tab); no `drawer` means closed; an invalid value is treated as `timeline`.
- G-FR-3b The tool filter is stored as `tool=<name>` (URL-encoded by `URLSearchParams`, at most 200 characters); it is honored only with `layout=graph` and `drawer=timeline`, and is dropped from the URL otherwise.
- G-FR-3c Query keys are written in the fixed order `layout`, `tools`, `drawer`, `tool`, and defaults are omitted, so equal states give equal URLs.
- G-FR-4 Switching layout keeps the selected agent and replaces the history entry (`location.replace`), so Back leaves the session instead of toggling; switching to the tree layout drops `drawer` and `tool`.
- G-FR-5 Selecting a card updates the agent segment of the hash with the same rules as the tree view (`buildSessionHash`), preserving `layout`, `tools` and `drawer`, and dropping `tool` when the agent changes.
- G-FR-6 Links to other sessions from the graph (session cards, header "continued from/in" links while in graph layout) carry `layout=graph`.
- G-FR-7 The SSE stream is opened with `graph=1` only while the graph layout is active; switching layout reopens the stream with or without it.

### 6.2 Data and live updates

- G-FR-8 On entering the graph layout the client fetches `GET /api/sessions/{source}/{sessionId}/graph` and renders it.
- G-FR-9 The client refetches `/graph` after the stream reports `hello` for the first time after mounting and after every resync, and takes the fetched graph unconditionally; this closes the race between the fetch and the subscription.
- G-FR-10 Within one stream connection the client applies a `graph.update` only if its `rev` is greater than the rendered `rev`; on `overflow: true` it refetches `/graph`.
- G-FR-11 The server builds the graph from the session's current tree, the per-agent stats, the per-agent pending tool calls and the session summaries, with the rules of section 8.3.
- G-FR-12 The server publishes `graph.update` for a session only to clients subscribed with that session and `graph=1`, only when the graph differs from the last published one, at most once per `GRAPH_MIN_INTERVAL`.
- G-FR-13 A change to the summary of a session in the continuation chain (title, status, agent count) also refreshes the graph of every session whose chain contains it.
- G-FR-14 `/graph` returns 200 while the session is still indexing, with `indexed: false` and the counts known so far; it never returns `not_ready`.

### 6.3 Canvas

- G-FR-15 The canvas fills the area of the tree and timeline panels; the drawer (G-FR-30) opens on its right.
- G-FR-16 Dragging on the background with the primary pointer pans; one-finger drag pans on touch; two-pointer pinch zooms around the midpoint.
- G-FR-17 The mouse wheel pans; `Ctrl` or `Cmd` plus wheel, and trackpad pinch (reported as `ctrlKey` wheel events), zoom around the pointer.
- G-FR-18 Zoom is clamped to `[0.1, 2.0]`; the toolbar has zoom out, zoom in, "100%", and "Fit" buttons, with the current zoom shown as a percentage.
- G-FR-19 On first render the view fits the whole graph if it fits at zoom 0.6 or more; otherwise it shows the root card at zoom 1.0 near the top center; if the URL selects an agent, that card is centered instead.
- G-FR-20 Wheel and pointer handlers act only inside the canvas and call `preventDefault` there, so the page does not scroll or zoom while interacting with the canvas.

### 6.4 Nodes and edges

- G-FR-21 Every agent of the session is an agent card; the root is labelled `main`.
- G-FR-22 Every session in the continuation chain other than the current one is a session card; predecessors are placed left of the root, successors right, nearest first.
- G-FR-23 Spawn edges connect each agent to its parent; edges of `linkedBy: orphan` agents are drawn dotted and the card shows the orphan warning.
- G-FR-24 Resume edges connect the resuming agent to the resumed agent, one edge per ordered pair with a count label when the count is above 1.
- G-FR-25 `continued_in` edges connect predecessor to successor across the session cards and the current root card.
- G-FR-26 Tool and skill nodes follow the tools mode of section 2.4; each shows its name, call count, and error count when above zero; a tool with pending calls while its agent is running is marked as active.
- G-FR-27 Each agent card has a collapse control when it has children; a collapsed card hides its descendants and their tool nodes and shows "+N agents" where N counts all hidden descendants; collapse state is kept in memory per session view and is not in the URL.

### 6.5 Selection and opening

- G-FR-28 Clicking a card, or pressing Enter or Space on a focused card, selects it and opens the drawer on it.
- G-FR-29 Clicking a tool node selects its owner agent and opens the drawer on the Timeline tab filtered to that tool (`drawer=timeline&tool=<name>`); clicking a skill or "+N more" node selects its owner agent and opens the drawer unfiltered; clicking a session card navigates to that session in graph layout.
- G-FR-29a With a tool filter active, the drawer timeline requests `/events?toolName=<name>` for every page (initial, older, newer), shows a chip "filtered: <name>" above the rows, and applies the same exact-match rule to `events.append` payloads so live events of other tools are not inserted.
- G-FR-29b The chip's clear button (and Backspace or Delete on the focused chip) removes the filter, reloads the unfiltered timeline, and removes `tool` from the URL with `location.replace`.
- G-FR-29c A filtered timeline with no matching events shows "No <name> calls in this agent yet." and the chip, not the generic empty state.
- G-FR-30 The drawer shows a header (agent label, type, status, short agentId with copy), two tabs "Timeline" and "Stats" reusing `createTimelineView` and `createStatsView` unchanged, a close button, and an "Open in tree view" button that switches the layout to `tree` with the same agent selected.
- G-FR-31 Escape closes the drawer and returns focus to the selected card; closing keeps the selection.
- G-FR-32 The drawer is open exactly when the URL has `drawer`; opening it (G-FR-28) writes `drawer=timeline` unless a tab is already set, switching tabs rewrites `drawer` with `location.replace`, and closing it removes `drawer` and `tool`; loading a URL with `drawer` opens it on the selected agent, including `main`.
- G-FR-33 Double-clicking a card zooms to fit that card's visible subtree.

### 6.6 Legend, notices and states

- G-FR-34 A legend, collapsed by default and toggled from the toolbar, explains card kinds, status colors and every edge style of section 10.4.
- G-FR-35 When new agents appear outside the viewport, a pill "N new agents" appears; clicking it pans to the newest one and clears it.
- G-FR-36 The canvas shows the empty, loading, error, indexing and truncated states of section 10.9.

### 6.7 Accessibility

- G-FR-37 The card layer has `role="tree"` and each agent card `role="treeitem"` with `aria-level`, `aria-selected` and, when it has children, `aria-expanded`; roving `tabindex` makes exactly one card tabbable.
- G-FR-38 Arrow keys follow the WAI-ARIA tree pattern and the existing tree view (SPEC 8.2): Up and Down move to the previous and next card in depth-first order, Right expands or moves to the first child, Left collapses or moves to the parent, Home and End go to the first and last card; they reuse `flattenTree` and `moveSelection` from `lib/tree.js`.
- G-FR-39 Moving focus pans the viewport so the focused card is fully visible.
- G-FR-40 Each card has an accessible description listing its relations and usage, for example "spawned by main; resumed 1 time by main; tools: Bash 1 call, 1 error; skills: none"; tool, skill and edge graphics are `aria-hidden`.
- G-FR-41 With the canvas focused, `+` and `-` zoom, `0` fits, `1` sets 100%, and Shift plus arrow keys pan by 100 screen pixels; these keys do not fire while focus is in the drawer or a form control.
- G-FR-42 A polite live region announces new agents and status changes of the selected agent, at most one announcement every 5 s, merged ("3 new agents").
- G-FR-43 Edge kinds differ by dash pattern and arrowhead, not by color alone; status is shown by a dot plus the status word in the card's accessible name.
- G-FR-44 Under `prefers-reduced-motion: reduce` no transition or pulse runs; running cards use a thicker border instead.

### 6.8 Minimap

- G-FR-45 The canvas has a minimap in its bottom-right corner, 200 x 140 px (160 x 112 px at 720 px width or less), showing the bounds of the whole visible graph scaled to fit with its aspect ratio kept.
- G-FR-46 Every visible node of the current layout is a filled rectangle: agent cards in their status color, session cards in `--muted`, tool and skill nodes in `--border` and `--skill`; the selected card is outlined in `--accent`; edges and text are not drawn.
- G-FR-47 A rectangle outlines the part of the graph currently visible in the canvas viewport.
- G-FR-48 Clicking a point in the minimap centers the canvas viewport on the corresponding world point without changing zoom; dragging the viewport rectangle (or dragging anywhere in the minimap) pans the canvas continuously; the canvas does not animate while dragging.
- G-FR-49 The minimap stays in sync: its viewport rectangle updates on every pan, zoom, fit and resize of the canvas within the same animation frame, and its node rectangles update on every applied `graph.update`, collapse, tools-mode change and layout change.
- G-FR-50 A toolbar button "Minimap" (with `aria-pressed`) and the key `m` with the canvas focused toggle it; it is shown by default above 720 px width and hidden by default at 720 px or less; its visibility is not in the URL.
- G-FR-51 The minimap is `aria-hidden` and not focusable, because every action it offers is available from the keyboard through G-FR-38, G-FR-39 and G-FR-41; it respects `prefers-reduced-motion` (no transitions of its rectangles).
- G-FR-52 The minimap never covers the selected card: when the selected card would be under it after an ensure-visible pan, the pan target is offset by the minimap size.

## 7. Non-functional requirements

- G-NFR-1 No new runtime dependency: backend stays Python stdlib on system `python3` (3.9 or newer), frontend stays plain ES modules with no build step, no npm packages and no CDN.
- G-NFR-2 The CSP of SPEC 11 is unchanged; the new stylesheet `static/graph.css` is same-origin; no inline `style` attribute, no inline script.
- G-NFR-3 Transcript-derived strings (descriptions, titles, tool names, skill names, agent names) enter the DOM only through the `dom.js` builders (`textContent`, text nodes, non-URL attributes); `banned_api.test.js` passes unchanged.
- G-NFR-4 The graph endpoint and builder are read-only and never open transcript files; `test_readonly_guard.py` passes unchanged.
- G-NFR-5 `graph.py` is a pure function with no I/O and no imports from `store.py`, `server.py` or `sse.py`, deterministic for the same inputs (shuffled dict insertion order gives an identical result).
- G-NFR-6 Unknown fields in `/graph` and `graph.update` are ignored by the client, as in SPEC 14.5.2.
- G-NFR-7 Performance budgets of section 9 hold.
- G-NFR-8 Files stay under 800 lines and functions under 50 lines; the canvas view is split across the modules of section 12.
- G-NFR-9 The tree layout, list view and every existing test are unaffected when the graph layout is never used; the two existing suites pass throughout.
- G-NFR-10 Line coverage of `agent_viewer/graph.py` is at least 90% and of the new pure JS modules at least 80% of lines exercised by `node --test`.

## 8. Data contract

### 8.1 Endpoint

`GET /api/sessions/{source}/{sessionId}/graph` (and `HEAD`).

- 200: Graph JSON (8.2).
- 400 `bad_request`: unknown source.
- 404 `not_found`: unknown session, with the error envelope of SPEC 9.
- No query parameters in v1; unknown parameters are ignored like the other endpoints.
- Headers as every `/api/*` response (SPEC 11), including `Cache-Control: no-store`.

### 8.2 Graph JSON

Top level:

| Field | Type | Meaning |
|---|---|---|
| `sessionKey` | str | `<source>:<sessionId>` |
| `rev` | int | per-session revision, starts at 1, incremented by the server each time the built graph changes; per server process |
| `rootKey` | str | agent key of the root, also the root card's node id |
| `indexed` | bool | the session summary's `indexed` |
| `truncated` | bool | true when tool or skill nodes were dropped by `maxNodes` |
| `limits` | object | `{toolsPerAgent, chainHops, maxNodes}` actually applied |
| `nodes` | list[Node] | ordered as in 8.3 rule 7 |
| `edges` | list[Edge] | ordered as in 8.3 rule 7 |

Node ids are opaque strings; the client must not parse them.
Agent node ids are agent keys so they join directly with tree node keys and with `events.append.agentKey`.

Node, common fields: `id`, `kind` in `agent`, `session`, `tool`, `skill`.

Agent node (`kind: "agent"`):

| Field | Type | Source |
|---|---|---|
| `key` | str | tree node `key` (equal to `id`) |
| `agentId` | str | tree node |
| `sessionKey` | str | owning session |
| `parentId` | str or null | tree `parentKey` |
| `depth` | int | tree |
| `isRoot` | bool | `key == rootKey` |
| `agentType`, `description`, `name` | str or null | tree |
| `status` | enum | tree (`running`, `idle`, `finished`, `stale`) |
| `linkedBy` | enum | tree |
| `warnings` | list[str] | tree |
| `missing`, `isFork`, `stoppedByUser` | bool | tree |
| `forkedSkill` | str or null | tree |
| `models` | list[str] | tree |
| `tokensTotal`, `eventCount` | int | tree |
| `spawnedAt`, `lastActivityAt` | ts or null | tree |
| `childCount` | int | length of tree `children` |
| `resumeCount` | int | length of tree `resumes` |
| `toolCalls` | int | sum of `calls` over agent-scope `stats.tools` |
| `relationToolCalls` | int | calls of `Agent`, `Task`, `task`, `SendMessage`, `Skill` |
| `errors` | object | agent-scope `stats.errors` (`toolErrors`, `apiErrors`, `aborted`) |
| `toolsOmitted`, `toolsOmittedCalls` | int | distinct tools and their calls not emitted as nodes |

Session node (`kind: "session"`), only for sessions other than the current one:

| Field | Type | Meaning |
|---|---|---|
| `sessionKey` | str | the linked session |
| `relation` | enum | `predecessor` or `successor` |
| `hops` | int | 1 for a direct neighbour |
| `title`, `status`, `startedAt`, `lastActivityAt`, `agentCount` | from the summary | `null` when `missing` |
| `missing` | bool | the key is referenced but no summary exists |

Tool node (`kind: "tool"`): `ownerId` (agent node id), `name`, `calls`, `errors`, `pending` (calls without a result), `totalDurationMs`.

Skill node (`kind: "skill"`): `ownerId`, `name`, `count`, `via` (`{tool, slash, fork, read, preload}`, from agent-scope `stats.skills`).

Edge: `id` (opaque), `kind`, `from`, `to` (node ids), plus per kind:

| `kind` | Extra fields | From -> to |
|---|---|---|
| `spawn` | `linkedBy`, `toolCallId` (tree `spawnToolCallId`), `timestamp` (child `spawnedAt`) | parent agent -> child agent |
| `resume` | `count`, `toolCallIds` (list), `lastAt` | resuming agent -> resumed agent |
| `continued_in` | none | earlier session -> later session (current session is its root agent node) |
| `uses_tool` | `count` (= calls), `errors` | agent -> tool node |
| `uses_skill` | `count` | agent -> skill node |

### 8.3 Build rules (`agent_viewer/graph.py`)

1. Agent nodes: one per tree node, visited depth-first from `rootKey` in `children` order; nodes not reachable from the root follow in key order (same rule as `flattenTree` in `static/lib/tree.js`).
2. Spawn edges: one per agent with a non-null `parentKey`.
3. Resume edges: tree `resumes` grouped by `(byAgentKey, target)`; `count` is the group size, `toolCallIds` in timestamp order, `lastAt` the latest timestamp; a `byAgentKey` that is not a node of this session is dropped and counted in nothing (resumes never cross sessions in SPEC 6.1).
4. Chain: follow `continuedFrom` from the current summary up to `CHAIN_HOPS = 5` times and `continuedIn` up to 5 times, stopping at a repeated key; a key with no summary yields a session node with `missing: true` and stops that direction.
5. Tools: from agent-scope `stats.tools`, drop the relation tools of section 2.4, order by `calls` descending then `name`, emit the first `TOOLS_PER_AGENT = 50`, sum the rest into `toolsOmitted` and `toolsOmittedCalls`; `pending` counts tool calls of that name whose status is `pending`.
6. Skills: one node per key of agent-scope `stats.skills`, ordered by `count` descending then `name`.
7. Order: agent nodes (rule 1), then session nodes (predecessors by hops, then successors by hops), then per agent in rule 1 order its tool nodes then its skill nodes; edges ordered by kind (`spawn`, `resume`, `continued_in`, `uses_tool`, `uses_skill`), then by the order of their `to` node.
8. Cap: if the node count would exceed `MAX_NODES = 2000`, tool and skill nodes are dropped from the end of the order until it fits, `truncated` is set, and each affected agent's `toolsOmitted` and `toolsOmittedCalls` include the dropped tools; agent and session nodes are never dropped.
9. Ids: agent `id` is the agent key; session `session|<sessionKey>`; tool `tool|<agentKey>|<name>`; skill `skill|<agentKey>|<name>`; edge `<kind>|<from>|<to>`.
10. The function is `build_graph(session_key, tree, summaries, usage_by_agent, limits=DEFAULT_LIMITS) -> dict`, where `tree` is the SPEC 9.4 dict, `summaries` maps session keys to SPEC 9.2 dicts, and `usage_by_agent` maps agent keys to `{"tools": stats.tools, "skills": stats.skills, "errors": stats.errors, "pending": {toolName: int}}`; `rev` is added by the store, not by the builder.

### 8.4 Example: `contract/examples/graph.json`

Consistent with `contract/examples/tree.json` (statuses and token totals) and with the agent-scope stats produced from `tests/fixtures/claude/-tmp-demo/` (tool and skill counts, verified by running the server on the fixtures on 2026-09-28).
Note that `main` has 4 tool calls, all relation tools, so it has a skill node and no tool node.

```json
{
  "sessionKey": "claude:s-main",
  "rev": 1,
  "rootKey": "claude:s-main:main",
  "indexed": true,
  "truncated": false,
  "limits": {"toolsPerAgent": 50, "chainHops": 5, "maxNodes": 2000},
  "nodes": [
    {"id": "claude:s-main:main", "kind": "agent", "key": "claude:s-main:main", "agentId": "main", "sessionKey": "claude:s-main", "parentId": null, "depth": 0, "isRoot": true, "agentType": "main", "description": null, "name": null, "status": "finished", "linkedBy": "root", "warnings": [], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": ["claude-opus-5-5"], "tokensTotal": 5235, "eventCount": 16, "spawnedAt": "2026-09-25T10:00:00.000Z", "lastActivityAt": "2026-09-25T10:00:32.100Z", "childCount": 3, "resumeCount": 0, "toolCalls": 4, "relationToolCalls": 4, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "claude:s-main:a1111111111111111", "kind": "agent", "key": "claude:s-main:a1111111111111111", "agentId": "a1111111111111111", "sessionKey": "claude:s-main", "parentId": "claude:s-main:main", "depth": 1, "isRoot": false, "agentType": "general-purpose", "description": "Helper one", "name": null, "status": "finished", "linkedBy": "meta", "warnings": [], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": ["claude-haiku-4-5-20251001"], "tokensTotal": 1521, "eventCount": 6, "spawnedAt": "2026-09-25T10:00:01.100Z", "lastActivityAt": "2026-09-25T10:00:05.000Z", "childCount": 1, "resumeCount": 1, "toolCalls": 1, "relationToolCalls": 1, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "claude:s-main:a2222222222222222", "kind": "agent", "key": "claude:s-main:a2222222222222222", "agentId": "a2222222222222222", "sessionKey": "claude:s-main", "parentId": "claude:s-main:a1111111111111111", "depth": 2, "isRoot": false, "agentType": "general-purpose", "description": "Nested child", "name": null, "status": "finished", "linkedBy": "meta", "warnings": [], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": ["claude-haiku-4-5-20251001"], "tokensTotal": 610, "eventCount": 4, "spawnedAt": "2026-09-25T10:00:02.500Z", "lastActivityAt": "2026-09-25T10:00:03.600Z", "childCount": 0, "resumeCount": 0, "toolCalls": 1, "relationToolCalls": 0, "errors": {"toolErrors": 1, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "claude:s-main:a3333333333333333", "kind": "agent", "key": "claude:s-main:a3333333333333333", "agentId": "a3333333333333333", "sessionKey": "claude:s-main", "parentId": "claude:s-main:main", "depth": 1, "isRoot": false, "agentType": "ecc:code-explorer", "description": "Helper legacy", "name": null, "status": "stale", "linkedBy": "transcript", "warnings": [], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": ["claude-sonnet-4-6"], "tokensTotal": 2, "eventCount": 2, "spawnedAt": "2026-09-25T10:00:01.200Z", "lastActivityAt": "2026-09-25T10:00:02.600Z", "childCount": 0, "resumeCount": 0, "toolCalls": 1, "relationToolCalls": 0, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "claude:s-main:a4444444444444444", "kind": "agent", "key": "claude:s-main:a4444444444444444", "agentId": "a4444444444444444", "sessionKey": "claude:s-main", "parentId": "claude:s-main:main", "depth": 1, "isRoot": false, "agentType": null, "description": null, "name": null, "status": "stale", "linkedBy": "orphan", "warnings": ["orphan"], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": [], "tokensTotal": 0, "eventCount": 1, "spawnedAt": "2026-09-25T10:01:00.000Z", "lastActivityAt": "2026-09-25T10:01:00.000Z", "childCount": 0, "resumeCount": 0, "toolCalls": 0, "relationToolCalls": 0, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "skill|claude:s-main:main|code-review", "kind": "skill", "ownerId": "claude:s-main:main", "name": "code-review", "count": 1, "via": {"tool": 1, "slash": 0, "fork": 0, "read": 0, "preload": 0}},
    {"id": "tool|claude:s-main:a2222222222222222|Bash", "kind": "tool", "ownerId": "claude:s-main:a2222222222222222", "name": "Bash", "calls": 1, "errors": 1, "pending": 0, "totalDurationMs": 200},
    {"id": "tool|claude:s-main:a3333333333333333|Read", "kind": "tool", "ownerId": "claude:s-main:a3333333333333333", "name": "Read", "calls": 1, "errors": 0, "pending": 1, "totalDurationMs": 0}
  ],
  "edges": [
    {"id": "spawn|claude:s-main:main|claude:s-main:a1111111111111111", "kind": "spawn", "from": "claude:s-main:main", "to": "claude:s-main:a1111111111111111", "linkedBy": "meta", "toolCallId": "toolu_A", "timestamp": "2026-09-25T10:00:01.100Z"},
    {"id": "spawn|claude:s-main:a1111111111111111|claude:s-main:a2222222222222222", "kind": "spawn", "from": "claude:s-main:a1111111111111111", "to": "claude:s-main:a2222222222222222", "linkedBy": "meta", "toolCallId": "toolu_N", "timestamp": "2026-09-25T10:00:02.500Z"},
    {"id": "spawn|claude:s-main:main|claude:s-main:a3333333333333333", "kind": "spawn", "from": "claude:s-main:main", "to": "claude:s-main:a3333333333333333", "linkedBy": "transcript", "toolCallId": "toolu_B", "timestamp": "2026-09-25T10:00:01.200Z"},
    {"id": "spawn|claude:s-main:main|claude:s-main:a4444444444444444", "kind": "spawn", "from": "claude:s-main:main", "to": "claude:s-main:a4444444444444444", "linkedBy": "orphan", "toolCallId": null, "timestamp": "2026-09-25T10:01:00.000Z"},
    {"id": "resume|claude:s-main:main|claude:s-main:a1111111111111111", "kind": "resume", "from": "claude:s-main:main", "to": "claude:s-main:a1111111111111111", "count": 1, "toolCallIds": ["toolu_M"], "lastAt": "2026-09-25T10:00:04.500Z"},
    {"id": "uses_tool|claude:s-main:a2222222222222222|tool|claude:s-main:a2222222222222222|Bash", "kind": "uses_tool", "from": "claude:s-main:a2222222222222222", "to": "tool|claude:s-main:a2222222222222222|Bash", "count": 1, "errors": 1},
    {"id": "uses_tool|claude:s-main:a3333333333333333|tool|claude:s-main:a3333333333333333|Read", "kind": "uses_tool", "from": "claude:s-main:a3333333333333333", "to": "tool|claude:s-main:a3333333333333333|Read", "count": 1, "errors": 0},
    {"id": "uses_skill|claude:s-main:main|skill|claude:s-main:main|code-review", "kind": "uses_skill", "from": "claude:s-main:main", "to": "skill|claude:s-main:main|code-review", "count": 1}
  ]
}
```

### 8.5 Example: `contract/examples/graph_chain.json`

A shape example for continuation, with synthetic keys `claude:s-chain-1`, `-2`, `-3`.
The committed fixtures contain no `continued-in` record, and adding one would change the session count asserted by `tests/e2e/test_smoke.py`, so continuation is covered with synthetic inputs to the pure builder and this example; `mock_server.py` does not serve it.

```json
{
  "sessionKey": "claude:s-chain-2",
  "rev": 1,
  "rootKey": "claude:s-chain-2:main",
  "indexed": true,
  "truncated": false,
  "limits": {"toolsPerAgent": 50, "chainHops": 5, "maxNodes": 2000},
  "nodes": [
    {"id": "claude:s-chain-2:main", "kind": "agent", "key": "claude:s-chain-2:main", "agentId": "main", "sessionKey": "claude:s-chain-2", "parentId": null, "depth": 0, "isRoot": true, "agentType": "main", "description": null, "name": null, "status": "running", "linkedBy": "root", "warnings": [], "missing": false, "isFork": false, "stoppedByUser": false, "forkedSkill": null, "models": ["claude-opus-5-5"], "tokensTotal": 1200, "eventCount": 9, "spawnedAt": "2026-09-25T12:00:00.000Z", "lastActivityAt": "2026-09-25T12:05:00.000Z", "childCount": 0, "resumeCount": 0, "toolCalls": 3, "relationToolCalls": 0, "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0}, "toolsOmitted": 0, "toolsOmittedCalls": 0},
    {"id": "session|claude:s-chain-1", "kind": "session", "sessionKey": "claude:s-chain-1", "relation": "predecessor", "hops": 1, "title": "Chain part one", "status": "finished", "startedAt": "2026-09-25T11:00:00.000Z", "lastActivityAt": "2026-09-25T11:59:00.000Z", "agentCount": 2, "missing": false},
    {"id": "session|claude:s-chain-3", "kind": "session", "sessionKey": "claude:s-chain-3", "relation": "successor", "hops": 1, "title": null, "status": null, "startedAt": null, "lastActivityAt": null, "agentCount": null, "missing": true},
    {"id": "tool|claude:s-chain-2:main|Read", "kind": "tool", "ownerId": "claude:s-chain-2:main", "name": "Read", "calls": 3, "errors": 0, "pending": 1, "totalDurationMs": 90}
  ],
  "edges": [
    {"id": "continued_in|session|claude:s-chain-1|claude:s-chain-2:main", "kind": "continued_in", "from": "session|claude:s-chain-1", "to": "claude:s-chain-2:main"},
    {"id": "continued_in|claude:s-chain-2:main|session|claude:s-chain-3", "kind": "continued_in", "from": "claude:s-chain-2:main", "to": "session|claude:s-chain-3"},
    {"id": "uses_tool|claude:s-chain-2:main|tool|claude:s-chain-2:main|Read", "kind": "uses_tool", "from": "claude:s-chain-2:main", "to": "tool|claude:s-chain-2:main|Read", "count": 3, "errors": 0}
  ]
}
```

### 8.6 SSE

Stream: `GET /api/stream?session=<source>:<sessionId>&graph=1`.
`graph` accepts `1`, `true`, `0`, `false` (the existing `parse_bool`); any other value is `400 bad_request`; `graph=1` without any `session` is allowed and has no effect.

New event, added to the table of SPEC 7.4:

| Event | Data | When |
|---|---|---|
| `graph.update` | `{"sessionKey", "rev", "graph": Graph}` or `{"sessionKey", "rev", "graph": null, "overflow": true}` | the built graph of a subscribed session changed; coalesced 250 ms, at most 1 per session per 1 s; only to clients with `graph=1` |

No other event changes; `tree.update`, `events.append`, `agent.reset`, `stats.update` and `session.upsert` keep their shapes and recipients.

Lines to append to `contract/examples/sse_script.jsonl`, each right after the matching existing step so the mock replays a consistent story (the `graph` objects are `graph.json` with the listed differences; they are written in full in the file, abbreviated here):

```jsonl
{"delayMs": 50, "event": "graph.update", "data": {"sessionKey": "claude:s-main", "rev": 2, "graph": "<graph.json with rev 2; a2222222222222222 status running, eventCount 5>"}}
{"delayMs": 50, "event": "graph.update", "data": {"sessionKey": "claude:s-main", "rev": 3, "graph": "<graph.json with rev 3; a2222222222222222 status finished, eventCount 4>"}}
{"delayMs": 1500, "event": "graph.update", "data": {"sessionKey": "claude:s-main", "rev": 4, "graph": null, "overflow": true}}
```

The first follows the first `tree.update` (where `a2222222222222222` becomes `running`), the second follows the second `tree.update`, the third exercises the overflow refetch path.
`mock_server.session_of` returns `data["sessionKey"]` for `graph.update`.

### 8.7 Store protocol addition

`Api` gains `store.graph(session_key) -> dict | None` (the Graph JSON including `rev`), documented in the `api.py` module docstring next to the existing protocol, and implemented by both `store.Store` and `contract/mock_server.ExampleStore` (which serves `graph.json` for `claude:s-main` and returns `None` otherwise).

`store.events` gains a keyword argument: `events(agent_key, before, after, limit, kinds, include_meta, tool_name=None) -> dict | None`, implemented by `store.Store` (passing it to `AgentState.events(..., tool_name=None)`) and by `ExampleStore` (filtering its canned items the same way).

### 8.8 Tool filter on `/events`

`GET /api/agents/{source}/{sessionId}/{agentId}/events` gains one optional query parameter, added to the parameter list of SPEC 9.1:

| Parameter | Type | Rule |
|---|---|---|
| `toolName` | str | keep only events whose `toolName` equals the value exactly (case-sensitive); empty means no filter; longer than 200 characters is `400 bad_request` with message "toolName is too long" |

- The filter is applied before paging, so `limit`, `before`, `after`, `fromSeq`, `toSeq`, `hasBefore` and `hasAfter` all refer to the filtered sequence.
- It combines with `kinds` and `includeMeta` by AND; with no `kinds`, it matches `tool_call` and `tool_result` events of that tool (skill and meta events carry no `toolName`).
- `total` keeps its current meaning in `store.Store`: the number of events of the agent, unfiltered.
  `contract/mock_server.ExampleStore` today returns the filtered count for `kinds`; GA aligns it to the unfiltered count as part of this feature.
- An unknown tool name is not an error: the result is an empty page with `fromSeq` and `toSeq` `null`.
- `events.append` SSE payloads are unchanged and unfiltered; the client applies the same exact-match rule to them (G-FR-29a).

Example `contract/examples/events_page_tool_filter.json`, the response of `GET /api/agents/claude/s-main/a2222222222222222/events?toolName=Bash` on the fixtures (verified on 2026-09-28 with the equivalent `kinds=tool_call,tool_result` query on the running fixture server):

```json
{
  "agentKey": "claude:s-main:a2222222222222222",
  "items": [
    {"seq": 1, "kind": "tool_call", "timestamp": "2026-09-25T10:00:03.200Z", "role": "assistant", "uuid": "d2", "parentUuid": "d1", "messageId": "msg_d1", "model": "claude-haiku-4-5-20251001", "preview": "{\"command\": \"false\"}", "truncated": false, "redacted": false, "toolCallId": "toolu_X", "toolName": "Bash", "isError": false, "spawnedAgentKey": null, "resultSeq": 2, "durationMs": 200},
    {"seq": 2, "kind": "tool_result", "timestamp": "2026-09-25T10:00:03.400Z", "role": "tool", "uuid": "d3", "parentUuid": "d2", "messageId": null, "model": null, "preview": "Exit code 1", "truncated": false, "redacted": false, "toolCallId": "toolu_X", "toolName": "Bash", "isError": true, "spawnedAgentKey": null}
  ],
  "fromSeq": 1,
  "toSeq": 2,
  "total": 4,
  "hasBefore": false,
  "hasAfter": false
}
```

### 8.9 Hash URL grammar

```
#/s/<source>/<sessionId>[/a/<agentId>][?layout=graph][&tools=none|all][&drawer=timeline|stats][&tool=<name>]
```

Examples:
- `#/s/claude/s-main?layout=graph` - graph, tools mode `selected`, drawer closed.
- `#/s/claude/s-main/a/a2222222222222222?layout=graph&drawer=timeline&tool=Bash` - graph with `a2222222` selected and its drawer timeline filtered to `Bash`.
- `#/s/claude/s-main/a/a1111111111111111?layout=graph&tools=all&drawer=stats` - all tool nodes, drawer on the Stats tab.

`parseHash` returns `{view: 'session', source, sessionId, agentId, layout, tools, drawer, tool}` with `layout` in `tree|graph`, `tools` in `none|selected|all`, `drawer` in `null|timeline|stats`, and `tool` a string or `null`, normalized by G-FR-2 to G-FR-3c.

## 9. Performance

Reference sizes observed on this machine on 2026-09-28: largest session 89 agents, depth 2, widest fan-out 30 children, 15 distinct tools, 1,893 tool calls; its tree JSON is 70 KB.
Design sizes: 300 agents, 50 distinct tools per agent, 10,000+ tool calls.

- G-PERF-1 `build_graph` for 300 agents with 20 distinct tools each runs in under 20 ms on this machine (`tests/perf/test_graph_budget.py`, run with `AGENT_VIEWER_PERF=1`).
- G-PERF-2 Building a graph costs O(agents + distinct tools + pending calls), never O(tool calls): `pending` per tool name comes from a new `AgentState.pending_by_tool()` that reads the existing pending-call index.
- G-PERF-3 `/graph` for the 89-agent reference session responds in under 100 ms; its payload with tools included stays under 300 KB.
- G-PERF-4 The store builds graphs only for sessions with at least one `graph=1` subscriber (`Hub.watched_graph_sessions()`), only when the session is dirty or a chain neighbour's summary changed.
- G-PERF-5 `graph.update` is limited to 1 per session per second and falls back to `overflow` above 512 KiB serialized.
- G-PERF-6 Client layout of 2,000 visible nodes completes in under 50 ms in `node --test` (asserted with a generous bound of 200 ms to avoid flakiness; the 50 ms target is reported, not asserted).
- G-PERF-7 Pan and zoom only rewrite the world transform, batched with `requestAnimationFrame`; no layout, no DOM creation while panning.
- G-PERF-8 When more than `CULL_THRESHOLD = 300` nodes are visible, only cards and edges intersecting the viewport plus one viewport of margin are in the DOM, recomputed at the end of a pan or zoom gesture and at most every 100 ms during it.
- G-PERF-9 Applying a `graph.update` diffs by node id and edge id and touches only added, removed and changed elements; applying an update of 10 changed nodes to a 500-node canvas takes under 16 ms.
- G-PERF-11 The minimap redraws its viewport rectangle by changing four attributes of one `rect` per animation frame, and rebuilds its node rectangles only when the layout changes; with 2,000 nodes a rebuild takes under 10 ms.
- G-PERF-12 A `toolName`-filtered events page of 200 over a 30 MB agent transcript responds within the existing 50 ms page budget of SPEC 12.
- G-PERF-10 Level of detail by zoom band: at zoom 0.6 or more full cards; from 0.3 to 0.6 compact cards (status dot, type, short id); below 0.3 status-colored blocks without text; tool and skill nodes and their edges are hidden below 0.45.

## 10. UI behavior

### 10.1 Toggle

- Placed in the session header, right of the title, as "Tree | Graph".
- In graph layout the grid of SPEC 8 (tree, timeline, stats panels) is replaced by the canvas and the drawer; the header, crumbs and session metadata stay.
- The timeline and stats views are not mounted in graph layout until the drawer opens, so a graph user of a large session does not fetch the root's last event page for nothing.

### 10.2 Canvas toolbar

Top-left overlay inside the canvas: zoom out, zoom percentage, zoom in, "100%", "Fit", tools mode select (`none`, `selected`, `all`), "Legend" toggle, "Minimap" toggle, and an "indexing" chip while `indexed` is false.

### 10.3 Card anatomy

Agent card, fixed size 240 x 88 px so layout needs no text measurement:
- Row 1: status dot, type (`main` for the root, `agentType`, or "unknown type"), short agentId (first 8 chars, full in the `title`), warning marker when `warnings` is non-empty, badges `fork`, `skill: <forkedSkill>`, `stopped`.
- Row 2: description truncated to one line with ellipsis (full in `title`); for the root, the session title.
- Row 3: first model (and "+N" when there are more), compact tokens, tool call count, error count in the error color when above zero, resume count badge.
- A collapse control ("-" or "+N agents") centered on the bottom edge when `childCount > 0`.
- Selected: accent outline 2 px; focused: the standard focus ring; running: pulsing border; missing: dashed border and muted text.

Session card, 240 x 64 px: relation label ("continued from" or "continued in"), title or short session id, status dot and word, agent count; `missing: true` shows "not found" and is not clickable.

Tool node, 160 x 26 px pill: tool name (truncated), "x<calls>", error count; active (pending while owner running) shows a spinner-free dot animation, static under reduced motion.
Skill node: same size, distinct shape (rounded rectangle with a notch) and the `--skill` color, "x<count>".
"+N more" node: same size as a tool pill, text "+N more (<calls> calls)".

### 10.4 Edge styles

| Edge | Stroke | Pattern | Arrow | Label |
|---|---|---|---|---|
| spawn | `--border` darkened, 1.5 px | solid; dotted when `linkedBy` is `orphan` | none (direction is top-down) | none |
| resume | `--accent`, 1.5 px | dashed 6 4 | arrowhead at the resumed agent | count when above 1 |
| continued_in | `--muted`, 2 px | long dash 10 4 | arrowhead at the later session | none |
| uses_tool | `--muted`, 1 px | solid | none | none (counts are on the node) |
| uses_skill | `--skill`, 1 px | dotted 2 3 | none | none |

Routing: spawn edges are orthogonal elbows from the parent's bottom center to the child's top center through the midpoint between rows; resume edges are cubic curves bowed to the right of both cards; tool and skill edges are short horizontal segments from the card's right edge to the node column.

### 10.5 Layout (implemented in `static/lib/graph_layout.js`)

1. Input: the visible graph (after collapse and tools mode), constants `CARD_W 240`, `CARD_H 88`, `TOOL_W 160`, `TOOL_H 26`, `TOOL_GAP 6`, `H_GAP 32`, `V_GAP 56`, `LEAF_WRAP 6`.
2. Each agent's box is its card, widened by `H_GAP/2 + TOOL_W` and heightened to fit its visible tool and skill column when it has one.
3. Children are placed directly below their parent (`parent.y + box height + V_GAP`), in `children` order.
4. A node whose visible children are all leaves and number more than `LEAF_WRAP` places them in a grid of `LEAF_WRAP` columns, row by row.
5. Otherwise each subtree's width is the maximum of its own box width and the sum of its children's subtree widths plus gaps; the parent is centered over its children's span.
6. Session cards sit on the root's row: predecessors to the left by increasing hops, successors to the right.
7. All coordinates are integers; the output is `{positions: Map<id, {x, y, w, h}>, bounds, edges: Map<id, {d}>}`, a pure function of its input.

### 10.6 Selection and opening

- Single click or Enter selects and opens the drawer (G-FR-28); the drawer width is 480 px on wide screens, a bottom sheet of 70% height at 720 px width or less.
- The drawer's timeline receives `events.append` and `agent.reset` for its agent and the stats view receives `stats.update`, through the same routing `views/session.js` uses today.
- The URL agent segment always reflects the selected card; opening the drawer on another card replaces the drawer content without closing it and drops any tool filter.
- The drawer header shows the tabs; when a tool filter is active, a chip "filtered: <name>" with a clear button (accessible name "Clear tool filter <name>") sits between the tabs and the timeline rows.
- Clicking another tool node of the same agent replaces the filter; clicking a tool node of another agent selects that agent and applies its filter.

### 10.6a Minimap

- Position: bottom-right corner of the canvas with an 8 px margin, over the canvas, under the toolbar and banners; the legend stays bottom-left so they never overlap.
- Content: `--panel` background with a 1 px `--border` border and 70% opacity until hovered; node rectangles per G-FR-46 at the minimap scale, with a minimum size of 2 x 2 px so tiny nodes stay visible; the viewport rectangle is a 1.5 px `--accent` outline with a 10% `--accent` fill.
- Scale: `min(minimapW / boundsW, minimapH / boundsH)` over the layout bounds padded by 5%, recomputed when the bounds change; the rectangles are placed with the same integer rounding as the layout.
- Interaction: `pointerdown` in the minimap centers the viewport on that point, `pointermove` with the pointer captured keeps panning, `pointerup` ends; the wheel over the minimap zooms the canvas around the viewport center.
- When the viewport contains the whole graph, the viewport rectangle is clamped to the minimap area.

### 10.7 Live-update animation

- Before applying a new layout, the view records the screen position of an anchor card (focused, else selected, else root) and adjusts the pan afterwards so the anchor stays put.
- Moved cards transition `transform` over 200 ms ease-out; new cards fade and scale in from 0.92 over 180 ms and keep a highlight ring for 2 s; removed cards (after `agent.reset` or a reparent) fade out over 150 ms.
- A status change animates the dot color; a changed tool count flashes the pill for 600 ms.
- New cards outside the viewport raise the "N new agents" pill (G-FR-35) instead of moving the viewport.
- Under `prefers-reduced-motion: reduce` every duration is 0 and there is no pulse.

### 10.8 Legend

The minimap is not in the legend; its colors are the legend's status and kind colors.


A collapsible panel at the bottom-left listing: agent card, session card, tool node, skill node, "+N more"; the status colors of `running`, `idle`, `stale`, `finished`; and each edge style of 10.4 drawn with the real stroke.

### 10.9 Empty and error states

| State | Shown |
|---|---|
| Loading | "Loading graph..." in the canvas center |
| Root only, no tools or skills | the root card and "No subagents, tools or skills yet. The graph updates live." |
| Tools mode `selected` with nothing selected that has tools | a hint in the toolbar "Select an agent to see its tools" |
| Indexing | "indexing" chip; counts marked as partial in card descriptions |
| Truncated | banner "Showing N of M tool nodes. Switch tools to none or selected, or use the tree view." with a "Tree view" button |
| `tools=all` over the guard | notice "Too many nodes for all tools; showing the selected agent only." |
| 404 | the existing error box with a link back to the session list |
| Network or 5xx | error box with "Retry" and "Switch to tree view" buttons |
| Stream down | the existing appbar connection indicator; the canvas keeps the last graph and resyncs on return (G-FR-9) |
| Filtered timeline with no match | the chip and "No <name> calls in this agent yet." (G-FR-29c) |
| `tool` in the URL without `drawer=timeline` or in the tree layout | the filter is ignored and removed from the URL with `location.replace` (G-FR-3b) |

## 11. Test plan

### 11.1 Backend unit (`python3 -m unittest discover -s tests`)

`tests/unit/test_graph.py` (new), against the contract tree and fixture stats as inputs:
- building from `contract/examples/tree.json` and the fixture agent stats yields exactly `contract/examples/graph.json` minus `rev`;
- relation tools are excluded from tool nodes and counted in `relationToolCalls`;
- resumes are grouped per ordered pair with counts, ids and `lastAt`;
- orphan spawn edge has `linkedBy: orphan`;
- continuation chain: predecessors and successors to 5 hops, stop on a cycle, `missing` session nodes, reproducing `graph_chain.json`;
- `TOOLS_PER_AGENT` cap with `toolsOmitted` and `toolsOmittedCalls`;
- `MAX_NODES` cap drops tool and skill nodes from the end, never agents, sets `truncated`;
- ordering and determinism: shuffled dict insertion order gives an identical result;
- Oh My Pi fixture: `omp:o-root` gives `main` with a `read` tool node and a `jira-integration` skill node via `read`, `task` counted as a relation tool, and a spawn edge to `Checker` with `linkedBy: parentSession`;
- no input raises: empty tree, missing stats, unknown fields.

`tests/unit/test_api.py` (extended): `/graph` 200 shape validated against `graph.json` with `contract_shape.py`, 404 for an unknown session, 400 for an unknown source, `HEAD` works, `/graph` returns 200 with `indexed: false` while indexing; `graph=` stream parameter parsing and 400 on a bad value.

`tests/unit/test_sse.py` (extended): a `graph=1` client receives `graph.update` for its session, a client without it does not, list events are unaffected, `watched_graph_sessions()` reflects subscriptions, overflow payload above `MAX_INLINE_GRAPH_BYTES`.

`tests/unit/test_store.py` (extended): `graph(key)` equals `build_graph` of the store's state; `rev` increments only on change; a new agent file, a status change and a new tool call each publish one `graph.update` after coalescing; no graph is built without a graph subscriber; a chain neighbour's summary change refreshes the graph.

`tests/unit/test_api.py` (extended for the tool filter): `/events?toolName=Bash` for `a2222222222222222` returns exactly `contract/examples/events_page_tool_filter.json`; `toolName` combined with `kinds=tool_result` returns only seq 2; paging with `limit=1` and `after`/`before` walks the filtered sequence and sets `hasBefore`/`hasAfter` over it; an unknown name returns an empty page with `null` seqs; an empty value means no filter; a 201-character value is `400 bad_request`; `total` stays the unfiltered count.

`tests/unit/test_filter_events.py` (new): `AgentState.events(..., tool_name=...)` over the fixture agents: exact and case-sensitive match, AND with `kinds` and `include_meta`, `before`/`after` bounds, and the Oh My Pi `read` tool on `omp:o-root:main`.

`tests/unit/test_mock_server.py` (new): `ExampleStore.events` applies `tool_name` like the real store and returns the unfiltered `total`.

`tests/unit/test_pending.py` (new): `AgentState.pending_by_tool()` counts calls without a result per tool name, drops them when the result arrives, and is empty after `reset()`.

### 11.2 Frontend unit (`node --test agent_viewer/static/tests/`)

- `graph_model.test.js`: visible-graph derivation from `graph.json` for each tools mode; top 5 plus "+N more" synthesis; collapse hides descendants and counts them; diff of two graphs reports added, removed and changed ids; accessible description text for each fixture agent.
- `graph_layout.test.js`: determinism; no two card boxes overlap for random trees of 1 to 500 nodes (seeded generator); children below parent and in order; leaf wrapping at more than 6 leaves; session cards left and right of the root; adding a last child moves no node outside its ancestors' subtrees and later siblings; integer coordinates; the 2,000-node timing bound of G-PERF-6.
- `viewport.test.js`: zoom around a point keeps that world point under the cursor; clamping; fit to bounds; screen-to-world round trip; "ensure visible" pan for a focused card; LOD band from zoom.
- `minimap.test.js` (GB): scale and offset for wide, tall and single-node bounds with aspect ratio kept; world-to-minimap and minimap-to-world round trip; the viewport rectangle for a given pan and zoom, and its clamping when the whole graph is visible; the pan target produced by a click and by a drag delta; the 2 x 2 px minimum node size; the offset rule of G-FR-52.
- `state.test.js` (extended by GC): `parseHash` returns `layout`, `tools`, `drawer` and `tool` with defaults; invalid values fall back; `tool` is dropped without `layout=graph` and `drawer=timeline`; `buildSessionHash` round-trips all four in the fixed order of G-FR-3c and omits defaults; tool names with spaces, `&`, `#`, `/` and `%` round-trip; the existing deepEqual assertions are updated to include the four new fields.
- `timeline_filter.test.js` (GC): the pure helper `matchesToolFilter(event, toolName)` in `lib/events.js` used for `events.append` payloads, exact and case-sensitive.
- `banned_api.test.js` passes unchanged over the new files.

### 11.3 HTTP e2e (`tests/e2e/test_smoke.py`, extended by GA)

After the existing steps: `GET /api/sessions/claude/s-main/graph` matches `graph.json` except `rev` and the time-dependent statuses; open `/api/stream?session=claude:s-main&graph=1`, append one assistant `tool_use` line (`Grep`) to the fixture copy of `agent-a2222222222222222.jsonl`, and assert one `graph.update` arrives within 3 s with a `Grep` tool node having `pending: 1`; the read-only checksum check still passes.

### 11.4 Browser e2e (`tests/e2e/test_ui_smoke.py`, extended by GC, skipped without Playwright)

Playwright for Python is not installed on the system interpreter today, so this test is skipped by default; it is also run manually with the Playwright MCP browser during review.
1. Open `claude:s-main`, click "Graph"; the URL gains `layout=graph`; 5 agent cards are visible.
2. The orphan card shows the warning marker; one dashed resume edge exists between `main` and `a1111111`.
3. Select `a2222222`: a `Bash` tool pill shows "x1" and one error.
4. Press Enter: the drawer opens with the timeline and the URL gains `drawer=timeline`; expand the Bash call and see "Exit code 1" as an error.
4a. Click the `Bash` pill: the URL gains `tool=Bash`, the chip "filtered: Bash" shows, and the timeline contains only the Bash call and result; reload restores the filtered view; clearing the chip removes `tool` from the URL and shows all 4 events.
4b. The minimap is visible, has one rectangle per visible node, and its viewport rectangle moves when the canvas is panned and shrinks when zoomed in; clicking the minimap's far corner pans the canvas there; pressing `m` hides it.
5. Keyboard: Up moves to `a1111111`, Left moves to `main`, Escape closes the drawer and focus returns to the card.
6. Reload: the graph layout and selected agent are restored from the URL.
7. Append a new subagent file and sidecar to the fixture copy: a new card appears without reload.
8. No `img` or `script` element exists inside the canvas or drawer, and no dialog was raised (fixture strings `<img src=x onerror=alert(1)>` and `<script>`).
9. No CSP violation is reported in the console.
10. At 390 px width the drawer is a bottom sheet and one-finger drag pans.

### 11.5 Manual checks

- Open the 89-agent real session on the running server at http://127.0.0.1:8765 and check the G-PERF budgets in the Chrome performance panel.
- Keyboard-only walkthrough and a screen reader pass (Orca on Linux) over the card descriptions.
- On the 89-agent session: drag the minimap viewport across the whole graph and confirm the canvas follows without dropped frames; open a `Bash` node of the busiest agent and confirm the filtered timeline pages correctly while the session is live.

## 12. Workstreams

Three workstreams with strict file ownership.
A file is edited only by its owner; a contract change (section 12.4) needs agreement from both sides and an edit to this document first.

### 12.1 GA: backend graph builder, endpoint, SSE

Creates:
- `agent_viewer/graph.py`
- `contract/examples/graph.json` (content of 8.4)
- `contract/examples/graph_chain.json` (content of 8.5)
- `contract/examples/events_page_tool_filter.json` (content of 8.8)
- `tests/unit/test_graph.py`
- `tests/unit/test_filter_events.py`
- `tests/unit/test_mock_server.py`
- `tests/unit/test_pending.py` (for `AgentState.pending_by_tool`)
- `tests/perf/test_graph_budget.py`

Edits:
- `agent_viewer/accumulate.py`: add `pending_by_tool() -> dict[str, int]`, and the `tool_name=None` keyword on `events()`; nothing else.
- `agent_viewer/store.py`: `graph(key)`, per-session `rev`, publishing `graph.update` from `_refresh_session`, chain-neighbour invalidation, `watched_graph_sessions` constructor argument, and `tool_name` passed through `events()`.
- `agent_viewer/sse.py`: `Client` graph flag, `wants()` rule for `graph.update`, `Hub.subscribe(sessions, graph=False)`, `Hub.watched_graph_sessions()`, `GRAPH_MIN_INTERVAL`, `MAX_INLINE_GRAPH_BYTES`, `cap_graph()`.
- `agent_viewer/api.py`: `/graph` route, `parse_stream_graph(query)`, the `toolName` parameter of `_events` with its length check, protocol docstring.
- `agent_viewer/server.py`: pass the graph flag from `_stream` to `hub.subscribe`.
- `agent_viewer/__main__.py`: wire `watched_graph_sessions=hub.watched_graph_sessions`.
- `contract/mock_server.py`: `ExampleStore.graph`, `session_of` for `graph.update`, `tool_name` in `ExampleStore.events`, and the unfiltered `total`.
- `contract/examples/sse_script.jsonl`: the lines of 8.6.
- `tests/unit/test_api.py`, `tests/unit/test_sse.py`, `tests/unit/test_store.py`, `tests/unit/contract_shape.py`, `tests/e2e/test_smoke.py`.
- `SPEC.md`: sole editor for this feature; adds the endpoint row and the `toolName` parameter to 9.1, the event row and the `graph` parameter to 7.4, the hash grammar of 8.9 to section 8, and a short section 8.5 pointing to this document (text for 8.5 supplied by GC).

Day 1 deliverable: `graph.json`, `graph_chain.json`, `events_page_tool_filter.json`, the `sse_script.jsonl` lines and `mock_server.py` serving `/graph`, honoring `toolName` and replaying `graph.update`, merged first so GB and GC work against the mock.

### 12.2 GB: canvas renderer, layout, pure libraries

Creates:
- `agent_viewer/static/lib/graph_model.js`: visible-graph derivation, "+N more" synthesis, collapse, diff, accessible descriptions.
- `agent_viewer/static/lib/graph_layout.js`: layout of 10.5.
- `agent_viewer/static/lib/viewport.js`: pan, zoom, fit, clamp, ensure-visible, LOD band.
- `agent_viewer/static/lib/minimap.js`: pure minimap math (scale, offsets, world and minimap conversions, viewport rectangle, click and drag pan targets, the G-FR-52 offset).
- `agent_viewer/static/views/minimap.js`: the minimap element, its pointer handling and its sync with the canvas, mounted by `views/graph.js`.
- `agent_viewer/static/views/graph.js`: the canvas view (world, toolbar, legend, input handling, keyboard, live region, culling, animation).
- `agent_viewer/static/views/graph_cards.js`: card, session card, tool, skill and "+N more" element builders and edge path elements.
- `agent_viewer/static/graph.css`: all canvas, card, edge, legend, minimap and animation styles, with reduced-motion rules.
- `agent_viewer/static/tests/graph_model.test.js`, `graph_layout.test.js`, `viewport.test.js`, `minimap.test.js`.

Edits:
- `agent_viewer/static/dom.js`: add one export `svg(tag, props, ...children)` using `createElementNS` with the same attribute refusals as `h` (additive; no change to `h`, `append`, `replace`, `setVar`).

Day 1 deliverable: `views/graph.js` exporting `createGraphView` with the full interface of 12.4.2 rendering a placeholder, so GC can wire it immediately.

### 12.3 GC: integration of toggle, routing, stream and drawer

Creates:
- `agent_viewer/static/views/drawer.js`: drawer shell hosting `createTimelineView` and `createStatsView`, tabs, the "filtered: <tool>" chip, close and "Open in tree view".
- `agent_viewer/static/tests/timeline_filter.test.js`.

Edits:
- `agent_viewer/static/lib/state.js`: `parseHash` returns `layout`, `tools`, `drawer` and `tool` for session routes; `buildSessionHash(source, sessionId, agentId, {layout, tools, drawer, tool})`.
- `agent_viewer/static/lib/events.js`: add the pure `matchesToolFilter(event, toolName)`.
- `agent_viewer/static/api.js`: `api.graph(source, sessionId)`, `toolName` in `api.events`, `'graph.update'` in `STREAM_EVENTS`, `openStream(sessions, handlers, {graph})`, an `onHello` handler call on every `hello`.
- `agent_viewer/static/views/timeline.js`: `load(ref, key, info, {toolName})` passes the filter to every page request, filters `events.append` with `matchesToolFilter`, and shows the G-FR-29c empty text; unfiltered behavior unchanged.
- `agent_viewer/static/app.js`: stream key includes the graph flag; route changes of `layout` and `tools` go to the mounted session view without remounting; forward `hello` to the view.
- `agent_viewer/static/views/session.js`: layout toggle, mounting the graph view and drawer or the existing grid, event routing (`graph.update` to the graph view, timeline and stats events to the drawer), refetch on hello and resync.
- `agent_viewer/static/index.html`: add `<link rel="stylesheet" href="/static/graph.css">`.
- `agent_viewer/static/style.css`: toggle and drawer styles only.
- `agent_viewer/static/tests/state.test.js`.
- `tests/e2e/test_ui_smoke.py`.

### 12.4 Interface contracts

#### 12.4.1 GA -> GB and GC (HTTP and SSE)

Sections 8.1 to 8.9, with `contract/examples/graph.json`, `graph_chain.json`, `events_page_tool_filter.json` and the `graph.update` lines of `sse_script.jsonl` as normative instances.
GB's pure tests read `contract/examples/graph.json` and `graph_chain.json` from disk with `node:fs`.
Development runs against `python3 contract/mock_server.py --port 8766`, then the real server with no code change.

#### 12.4.2 GB -> GC (JavaScript)

```js
// views/graph.js
export function createGraphView(container, {
  toolsMode,                  // 'none' | 'selected' | 'all', initial value from the URL
  onSelect,                   // (agentKey, { open: boolean, replace: boolean, toolName: string | null }) => void
                              //   open: true for click/Enter; replace: true for keyboard moves;
                              //   toolName: set when a tool node was clicked (open is then true)
  onNavigateSession,          // (sessionKey) => void, from a session card
  onToolsModeChange,          // (mode) => void, from the toolbar
  onRequestTreeView,          // () => void, from the truncated banner or error box
}) => ({
  setGraph(graph, { reset }), // full snapshot; reset: true after a (re)fetch, skips rev check and animation
  rev(),                      // rev of the rendered graph, or 0
  setSelected(agentKey),      // highlight, roving tabindex, ensure visible; does not call onSelect
  setToolsMode(mode),
  focusSelected(),            // move DOM focus to the selected card (after the drawer closes)
  fit(),
  setMinimapVisible(bool),    // GC never needs it; toolbar and `m` handle it inside GB, exposed for tests
  showError(err),             // renders the error state with Retry and tree view buttons
  destroy(),
});
```

```js
// views/drawer.js (GC internal, listed so GB knows the canvas shares the row with it)
export function createDrawer(container, { onClose, onOpenInTree, onTabChange, onClearFilter }) => ({
  open(agentRef, agentKey, treeNode, { tab: 'timeline' | 'stats', toolName: string | null }), close(), isOpen(),
  onEvent(name, data), resync(), destroy(),
});
```

`views/session.js` keeps loading the session detail (with `tree`) and handling `tree.update` in graph layout, and passes tree nodes to the drawer, so the drawer does not depend on the graph node shape.
`views/session.js` is the only writer of the URL: GB reports intent through the callbacks above and GC turns it into hashes with `buildSessionHash`.
The minimap lives entirely inside GB's canvas; GC only provides the container size.

### 12.5 Merge order

1. GA day-1 contract commit (examples and mock).
2. GB placeholder `createGraphView` and GC routing, toggle and drawer, in any order.
3. GA backend, GB renderer and GC integration complete independently; each keeps both suites green.
4. Final: GC's browser test and GA's e2e step, then the manual checks of 11.5.

## 13. Acceptance criteria

- AC-G1 Both suites pass: `python3 -m unittest discover -s tests` and `node --test agent_viewer/static/tests/`, including all tests of section 11.
- AC-G2 `GET /api/sessions/claude/s-main/graph` on the fixtures returns the content of `contract/examples/graph.json` except `rev` and time-dependent statuses; the mock server returns it verbatim.
- AC-G3 A tab in graph layout receives `graph.update` within 2 s of a transcript change that adds an agent, changes a status or adds a tool call; a tab in tree layout receives none.
- AC-G4 The toggle switches between tree and graph without a reload, the URL carries `layout=graph` and `tools=<mode>` when not default, and reloading or sharing the URL restores layout, tools mode and selected agent.
- AC-G5 The canvas pans by drag and wheel, zooms by Ctrl or Cmd plus wheel, pinch and the toolbar within `[0.1, 2.0]`, and "Fit" shows the whole graph.
- AC-G6 Every relation of `graph.json` is drawn with the style of 10.4: 4 spawn edges (one dotted), 1 resume edge, the `code-review` skill node on `main`, and the `Bash` and `Read` tool nodes with their counts when tools mode is `all`.
- AC-G7 Clicking or pressing Enter on any card opens the drawer with that agent's timeline and stats; Escape closes it and focus returns to the card; "Open in tree view" switches layout with the same agent selected.
- AC-G8 Keyboard-only use covers every action of US-7, following G-FR-37 to G-FR-41.
- AC-G9 Live updates keep the anchor card fixed on screen, animate as in 10.7, and show no animation under reduced motion.
- AC-G10 The G-PERF budgets hold; the 89-agent real session pans at a steady frame rate with tools mode `all`.
- AC-G11 No CSP violation, no external request, `banned_api.test.js` and `test_readonly_guard.py` pass, and the fixture's `<script>` and `<img onerror>` strings render as text in cards and drawer.
- AC-G12 The tree layout behaves exactly as before; the existing browser and e2e tests pass unchanged apart from the documented `parseHash` shape change.
- AC-G13 Usable at 1280 x 800 and at 390 px width.
- AC-G14 Clicking a tool node opens the drawer timeline showing only that tool's calls and results, fetched with `toolName`, with a "filtered: <tool>" chip; clearing the chip shows the full timeline; live events of other tools do not appear while filtered.
- AC-G15 `/events?toolName=` behaves as 8.8 on the real server and the mock server, validated against `contract/examples/events_page_tool_filter.json`.
- AC-G16 The URL carries `drawer` and `tool`; reloading or opening a shared URL restores the open drawer, its tab and its tool filter; closing the drawer or switching to the tree layout removes them.
- AC-G17 The minimap shows every visible node and the viewport rectangle, stays in sync within one frame on pan, zoom and fit and on every applied `graph.update`, pans the canvas on click and drag, toggles from the toolbar and with `m`, and is shown by default above 720 px width only.

## 14. Risks

- Wide fan-out: a node with 30 non-leaf children makes a very wide canvas; leaf wrapping covers the observed case (30 leaves), and collapse plus Fit cover the rest; a left-to-right orientation is a possible v2 control.
- Layout shifts on live updates can still disorient even with anchoring; the stable ordering by `spawnedAt` and appending new children last keep most changes local.
- Snapshot SSE costs bandwidth on large running sessions (estimated 100 to 200 KB per second for the 89-agent session with tools); acceptable on localhost, bounded by the 1 s limit and the overflow fallback; a patch format is a possible v2 optimization.
- Refetch race on connect is closed by refetching on `hello` (G-FR-9); a missed update is repaired by the next change or resync.
- Tool names come from transcripts and can be long MCP names such as `mcp__plugin_ecc_chrome-devtools__take_screenshot`; pills truncate them with the full name in `title`.
- Oh My Pi skill use is a `read` tool call with a `skill://` path, so it appears both as a `read` tool call and as a skill node; this double count mirrors the existing stats and is documented in the legend.
- The transcript schemas are undocumented (SPEC 16); the graph adds no parsing, so it inherits, and does not add to, that risk.
- `rev` resets when the server restarts; clients take the refetched graph unconditionally after a resync, so this is harmless.
- The tool filter scans an agent's event index linearly like `kinds` does today; for a filter that matches rarely in a very long transcript a page may scan the whole index, which G-PERF-12 bounds and `test_index_budget.py`-style perf checks watch.
- Tool names in the URL are transcript-derived; they are only ever compared as strings and rendered through `textContent`, never used as HTML, selectors or URLs.

## 15. Decisions log

All open questions of draft 1 were answered by the user on 2026-09-28; there are no open questions left.

| # | Question | Decision |
|---|---|---|
| 1 | Where the layout choice is remembered | URL only (`layout=graph`); "Tree" is the default; no `localStorage` |
| 2 | Default tools mode | `all` (changed from `selected` on 2026-09-28 at the user's request; `selected` remains available and is the fallback above the node guard) |
| 3 | Relation tools as tool nodes | Hidden; shown only as spawn and resume edges and skill nodes |
| 4 | Tool node granularity | Per agent |
| 5 | Multi-session canvas | Later version, not v1 |
| 6 | Tool node click filters the timeline | Yes, in v1: `toolName` on `/events` (8.8), drawer chip, `tool=` in the URL |
| 7 | Drawer state in the URL | Yes: `drawer=timeline|stats` (G-FR-32) |
| 8 | `irc:incoming` and cross-session spawn edges | Later version, not v1 |
| 9 | Minimap | Yes, in v1 (section 6.8, 10.6a) |
