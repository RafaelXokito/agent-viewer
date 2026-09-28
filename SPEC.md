# agent-viewer - Product and Technical Specification

Status: draft 1, 2026-09-28.
Scope: specification only; no implementation exists yet.

## 1. Summary

agent-viewer is a local, read-only web platform for watching AI coding agents work.
It reads the JSONL transcripts that Claude Code and Oh My Pi already write to disk, and shows them live in a browser on the same machine.
It shows running and recently finished sessions, the agent tree of each session, the full content of any session or subagent, and usage statistics.
It never writes to, moves, renames, locks or deletes any transcript file.

## 2. Goals

- G1 Live list: show sessions that are running now and sessions that finished recently, updating within 2 seconds of a transcript file growing.
- G2 Agent tree: show main session -> subagents -> nested subagents to any depth, with `sessionId` and `agentId` on every node.
- G3 Read any transcript: user prompts, assistant text, thinking (when the text is present), tool calls with inputs and results, skill invocations, system and notification records.
- G4 Stats per session and per agent: tool counts, skill invocations, subagent types spawned, models, tokens (input, output, cache read, cache creation), duration, errors.
- G5 Two sources behind one normalized model: Claude Code and Oh My Pi.
- G6 Robustness: a malformed, truncated or unknown record is skipped and counted, never fatal.
- G7 Strictly read-only access to transcript files.

## 3. Non-goals

- No writing, editing, replaying, resuming or controlling agents.
- No remote access, multi-user support, accounts or authentication.
- No persistent database or on-disk cache; all indexes live in memory and are rebuilt at start.
- No Markdown or HTML rendering of transcript content; content is shown as escaped plain text.
- No cost estimation in currency; Oh My Pi's recorded `cost.total` is shown as-is when present, Claude Code has no per-message cost.
- No sources other than Claude Code and Oh My Pi in v1, but the parser interface allows adding one.
- No reading of `tool-results/`, `tasks/*.output`, `file-history-snapshot` bodies, or the Oh My Pi `*.bash.log`, `*.json` and `*.md` sidecars in v1.

## 4. Source schemas (verified)

All facts in this section were verified on 2026-09-28 against real files on this machine with `head`, `grep` and `jq`.
Claude Code version observed: `2.1.282`.
Both schemas are undocumented and can change between CLI versions; every parser must tolerate missing fields and unknown record types.

### 4.1 Claude Code

Layout under `~/.claude/projects/` (the "Claude root"):

```
<project-slug>/
  <sessionId>.jsonl                               main session transcript
  <sessionId>/subagents/agent-<agentId>.jsonl     subagent transcript (flat folder, all depths)
  <sessionId>/subagents/agent-<agentId>.meta.json subagent sidecar (present for all 157 subagents observed)
  <sessionId>/subagents/agent-<agentId>.forked-skill.json          forked-skill sidecar (rare)
  <sessionId>/subagents/agent-<agentId>.forked-skill.marker.json   forked-skill marker (rare)
  <sessionId>/tool-results/...                    large tool outputs (ignored in v1)
  memory/                                         auto-memory (ignored)
```

`<project-slug>` is the absolute cwd with `/` and `.` replaced by `-`, for example `-home-alice-src-my-project`.
The slug is lossy, so the display name of a project is taken from the `cwd` field of its records, not from the slug.

Common fields on conversational records (`user`, `assistant`, `attachment`, `system`):
`type`, `uuid`, `parentUuid`, `timestamp` (ISO 8601, `Z`), `sessionId`, `isSidechain`, `cwd`, `gitBranch`, `version`, `entrypoint`, `userType`, `sessionKind`.
Subagent files additionally carry `agentId` on every line and `isSidechain: true`, and their `sessionId` is the parent main session's id.
No line of a subagent transcript records the parent agent.

`.meta.json` sidecar, observed shapes (one per line):

```jsonl
{"agentType":"general-purpose","description":"Wait 10s #1","toolUseId":"toolu_014J...","spawnDepth":1,"requestShape":"background","requestNonInteractive":true,"model":"haiku"}
{"agentType":"general-purpose","description":"watcher test child","toolUseId":"toolu_01BY...","parentAgentId":"afedcba9876543210","spawnDepth":2,"requestShape":"background","requestNonInteractive":true}
{"agentType":"fork","description":"...","name":"...","isFork":true,"spawnDepth":1}
{"agentType":"general-purpose","description":"/code-review low ...","name":"code-review","spawnDepth":1,"requestShape":"background","requestNonInteractive":true}
```

- `parentAgentId` is present exactly when `spawnDepth >= 2` (verified on 60 depth-2 sidecars; its `toolUseId` appears in the parent agent's transcript).
- `toolUseId` is the id of the `Agent` `tool_use` block that spawned the agent; it is absent for forks and named skill agents.
- `stoppedByUser: true` appears on agents the user stopped.
- The sidecar is the primary tree source; transcript scanning (section 6) is the fallback for files without a sidecar and a cross-check.

Record `type` values observed (40 largest files plus samples):
`user`, `assistant`, `attachment`, `system`, `queue-operation`, `last-prompt`, `ai-title`, `custom-title`, `agent-name`, `mode`, `permission-mode`, `atis-latch`, `bridge-session`, `pr-link`, `cost-state`, `file-history-snapshot`, `file-history-delta`, `history-suppression`, `frame-link`, `continued-in`.
No `summary` record type was observed in current files; the parser still accepts it (older CLI versions wrote `{"type":"summary","summary":"...","leafUuid":"..."}`).

`assistant` record:
- `message.id` (`msg_...`), `message.model` (for example `claude-opus-5-5`), `message.stop_reason` (`tool_use`, `end_turn`, `stop_sequence`, or `null`), `message.content[]`, `message.usage`, plus top-level `requestId`.
- One API response is split across several lines, one content block per line, all sharing `message.id` and repeating an identical `usage` object.
  Usage must therefore be counted once per `message.id`, never once per line.
- `usage` fields used: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`; also present and optional: `output_tokens_details.thinking_tokens`, `server_tool_use`, `service_tier`, `cache_creation.ephemeral_1h_input_tokens`, `iterations[]`.
- `message.model == "<synthetic>"` marks CLI-generated placeholder messages; they carry no spend and are excluded from usage.
- `isApiErrorMessage: true` marks an API error rendered as assistant text (for example "API Error: Connection lost mid-response.").
- Content block types: `text` (`text`), `thinking` (`thinking`, `signature`; the `thinking` string is empty in about 87% of observed blocks, meaning redacted), `tool_use` (`id`, `name`, `input`, `caller`).

`user` record:
- `message.content` is either a string (a human prompt, a slash command wrapper, or a notification) or an array of blocks.
- Array blocks observed: `tool_result` (`tool_use_id`, `content` as string or array of `text`/`image` blocks, optional `is_error`) and `text`.
- `toolUseResult` is a structured copy of the tool outcome; shapes that matter:
  - Agent spawn: `{"isAsync":true,"status":"async_launched","agentId":"a0123456789abcdef","description":"...","resolvedModel":"claude-opus-5-5","prompt":"...","outputFile":"...","canReadOutputFile":true}`.
  - SendMessage resume: `{"success":true,"message":"Resuming agent a012345","resumedAgentId":"a0123456789abcdef","pin":{...}}`.
  - Skill: `{"success":true,"commandName":"harness-adapters"}`.
- The `tool_result` text of an Agent spawn contains `agentId: <id>`, and of a resume contains `"resumedAgentId":"<id>"`.
- `isMeta: true` with `sourceToolUseID` marks injected content, for example the skill body that follows a `Skill` call ("Base directory for this skill: ...").
- String content starting with `<task-notification>` is a background-agent completion notice with `<task-id>` (the agentId), `<tool-use-id>` and `<status>` (for example `completed`).
- Slash commands appear as string content containing `<command-name>/model</command-name>`, `<command-message>` and `<command-args>`.
- `isCompactSummary: true` marks the synthetic summary written after context compaction.

Skill invocations appear in three forms:
1. `tool_use` with `name: "Skill"` and `input: {"skill": "<name>"}` (optionally `args`), result `toolUseResult.commandName`.
2. A slash command `<command-name>/<name></command-name>` whose name matches a skill (built-in commands such as `/model`, `/clear`, `/login` are recorded as slash commands, not skills; see 5.6).
3. A forked-skill subagent, identified by `agent-<id>.forked-skill.json` (`{"skillName":"code-review","attributionName":"code-review","effort":"low"}`).

`system` record subtypes observed: `turn_duration` (`durationMs`, `messageCount`), `stop_hook_summary`, `informational` (`level`), `local_command`, `away_summary`, `compact_boundary`; `level` may be `error`.

Session continuation: a `continued-in` record (`{"type":"continued-in","sessionId":"86c3...","continuedInSessionId":"e5f6...","timestamp":"..."}`) links a session to its successor.
The successor file re-contains history from the predecessor, and a subagent spawned before the hand-off keeps its file under the predecessor's `<sessionId>/subagents/` folder while the spawn record also appears in the successor.
Verified with agent `a0123456789abcdef`: file under `a1b2c3d4.../subagents/`, spawn result in `e5f6a7b8....jsonl`.

Resume: a resumed agent keeps appending to the same `agent-<agentId>.jsonl`; the resume arrives as a `user` record starting "The coordinator sent a message while you were working:".

Session-level metadata records: `ai-title.aiTitle`, `custom-title.customTitle` (wins over `ai-title`), `agent-name.agentName`, `last-prompt.lastPrompt`, `pr-link.prUrl`, `cost-state` (`totalCostUSD`, `totalDuration`, `modelUsage`), `permission-mode.permissionMode`.

### 4.2 Oh My Pi

Discovery follows the layout below, as observed on real Oh My Pi installations.
Layout under `~/.omp/agent/sessions/` (the "Oh My Pi root"):

```
<project-dir>/                                   for example -src-my-project
  <ISO-timestamp>_<sessionId>.jsonl              root session
  <ISO-timestamp>_<sessionId>/                   folder named after the root file stem
    <AgentName>.jsonl                            child (subagent) session
    <AgentName>.json, <AgentName>.md             result sidecars (ignored in v1)
    <n>.bash.log, <n>.bash-original.log          tool logs (ignored)
```

Only depth 1 children were observed; deeper children are expected at `<stem>/<AgentName>/<GrandChild>.jsonl` and must be found by recursive glob plus `parentSession`.

Record `type` values observed: `session`, `session_init`, `title`, `title_change`, `model_change`, `thinking_level_change`, `service_tier_change`, `message`, `custom`, `custom_message`.
Every record except `session` and `title` has `id` (8 hex chars), `parentId` and `timestamp`.

- `session`: `{"type":"session","version":3,"id":"01900000-0000-7000-8000-000000000003","timestamp":"...","cwd":"/home/.../my-project","title":"...","parentSession":"/abs/path/to/parent.jsonl"}`; `parentSession` exists only on children.
- `title`: first line of the file, with a `pad` field of spaces; this suggests the line is rewritten in place at a fixed width (inferred, not verified).
- `session_init`: child sessions only, carries the full `systemPrompt`.
- `model_change`: `model` as `provider/model`, `role`.
- `message` wraps `message.role` in `user`, `assistant`, `toolResult`.
  - `user`: `content[]` of `text` blocks, `attribution` (`user`).
  - `assistant`: `content[]` of `text`, `thinking` (`thinking` text, usually present), `toolCall` (`id`, `name`, `arguments`, `intent`); plus `model`, `provider`, `api`, `stopReason` (`toolUse`, `stop`, `aborted`), `errorMessage`, `duration` (ms), `ttft` (ms), `usage`.
  - `usage`: `input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, `reasoningTokens`, `cost.total`, `premiumRequests`; one record per API response, so no de-duplication is needed.
  - `toolResult`: `toolCallId`, `toolName`, `isError`, `content[]`, `details`.
- `custom` with `customType`: `tool_execution_start` (`data.toolCallId`, `data.toolName`, `data.startedAt`, `data.args`) and `session_exit` (`data.reason`, `data.kind` in `normal`/`signal`); a session may have several `session_exit` records if it was reopened.
- `custom_message` with `customType: "irc:incoming"`: a message from another agent (for example the parent) delivered into this session.
- Tool names observed: `read`, `bash`, `hub`, `glob`, `grep`, `todo`, `edit`, `eval`, `yield`, `task`.
- Subagent spawn: `toolCall` with `name: "task"`; its `toolResult.details.progress[]` lists `{id, agent, status, task, ...}` where `id` equals the child file stem (for example `EquivalenceChecklistTester`) and `agent` is the agent type (for example `reviewer`).
- Skill use: `read` with `arguments.path` starting `skill://` (for example `skill://jira-integration`).
- No `gitBranch` field exists; agent-viewer does not guess one from the checked-out branch (branch is `null` for Oh My Pi).

## 5. Normalized data model

Defined in `agent_viewer/model.py` as frozen dataclasses with a `to_dict()` that produces exactly the JSON shapes in section 9.
All timestamps are ISO 8601 UTC strings in JSON and `datetime` in Python.
All token counts are integers, defaulting to 0 when absent.

### 5.1 Keys

- `source`: `"claude"` or `"omp"`.
- `sessionId`: Claude `sessionId`; Oh My Pi root `session.id`.
- `agentId`: `"main"` for the root agent of a session; Claude `agentId` for subagents; Oh My Pi child path relative to the root folder without `.jsonl`, with `/` between levels (for example `EquivalenceChecklistTester`).
- Session key string: `<source>:<sessionId>`.
- Agent key string: `<source>:<sessionId>:<agentId>`.
- In URLs each part is a separate path segment, percent-encoded (`/` in an Oh My Pi `agentId` becomes `%2F`); the router splits on `/` before decoding.

### 5.2 Session

| Field | Type | Claude | Oh My Pi |
|---|---|---|---|
| `source` | str | `"claude"` | `"omp"` |
| `sessionId` | str | file stem of `<sessionId>.jsonl` | root `session.id` |
| `project` | str | project slug directory name | project directory name |
| `cwd` | str or null | first record `cwd` | root `session.cwd` |
| `gitBranch` | str or null | last non-empty `gitBranch` | `null` |
| `title` | str or null | `custom-title.customTitle`, else `ai-title.aiTitle`, else first prompt (120 chars) | last `title_change.title`, else `session.title` |
| `startedAt` | ts | first record `timestamp` | `session.timestamp` |
| `lastActivityAt` | ts | max record `timestamp` over main and all subagent files | same, over root and children |
| `mtime` | ts | max file mtime over main and subagent files | max over root and children |
| `status` | enum | section 7.3 | section 7.3 |
| `rootAgentKey` | str | `claude:<sid>:main` | `omp:<sid>:main` |
| `agentCount` | int | 1 + subagents | 1 + children |
| `continuedFrom` / `continuedIn` | str or null | from `continued-in` records | `null` |
| `version` | str or null | last `version` | root `session.version` |
| `prUrl` | str or null | `pr-link.prUrl` | `null` |
| `parse` | ParseStats | summed over files | summed over files |

### 5.3 Agent (tree node)

| Field | Type | Claude | Oh My Pi |
|---|---|---|---|
| `key` | str | agent key | agent key |
| `agentId` | str | `main` or `agentId` | `main` or relative stem path |
| `sessionId` | str | session | root session |
| `parentKey` | str or null | section 6.1 | section 6.2 |
| `depth` | int | 0 for main | 0 for root |
| `agentType` | str or null | meta `agentType`, else the spawning call's `input.subagent_type`; `main` for root | `details.progress[].agent`; `main` for root |
| `description` | str or null | meta `description`, else Agent `input.description` | task `intent`, else `null` |
| `name` | str or null | meta `name` | file stem |
| `linkedBy` | enum | `meta`, `transcript`, `orphan`, `root` | `parentSession`, `folder`, `orphan`, `root` |
| `spawnToolCallId` | str or null | meta `toolUseId` | task `toolCall.id` when resolvable |
| `spawnedAt` | ts or null | timestamp of the spawning tool call, else first record | child `session.timestamp` |
| `lastActivityAt` | ts | max `timestamp` in its file | same |
| `status` | enum | section 7.3 | section 7.3 |
| `stoppedByUser` | bool | meta `stoppedByUser` | `false` |
| `isFork` | bool | meta `isFork` | `false` |
| `forkedSkill` | str or null | `forked-skill.json.skillName` | `null` |
| `resumes` | list[Resume] | section 6.1 step 5 | `[]` |
| `models` | list[str] | distinct `message.model`, excluding `<synthetic>` | distinct `message.model` |
| `file` | str | absolute path | absolute path |
| `eventCount` | int | events indexed | events indexed |
| `missing` | bool | `true` for a placeholder whose file does not exist | same |
| `children` | list[str] | child agent keys ordered by `spawnedAt`, then key | same |

Resume: `{byAgentKey, toolCallId, timestamp}`.

### 5.4 Event

An Event is one normalized, displayable unit in an agent's timeline.
One source line produces zero or more Events, one per content block.

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | 0-based position in the agent's timeline; stable while the file only grows |
| `agentKey` | str | owner |
| `kind` | enum | `prompt`, `text`, `thinking`, `tool_call`, `tool_result`, `skill`, `notification`, `system`, `compaction`, `error`, `meta` |
| `timestamp` | ts or null | record `timestamp` |
| `role` | str or null | `user`, `assistant`, `tool`, `system` |
| `uuid` / `parentUuid` | str or null | Claude `uuid`/`parentUuid`; Oh My Pi `id`/`parentId` |
| `messageId` | str or null | Claude `message.id`; Oh My Pi `responseId` |
| `model` | str or null | assistant model |
| `preview` | str | first 2,000 characters of the text content |
| `truncated` | bool | `true` when `preview` is shorter than the full text |
| `toolCallId` | str or null | for `tool_call`, `tool_result`, `skill` |
| `toolName` | str or null | for `tool_call`, `tool_result` |
| `isError` | bool | tool result error, API error, aborted stop |
| `spawnedAgentKey` | str or null | for an `Agent`/`task` tool call whose child is known |
| `ref` | `{file, offset, length, block}` | location used to fetch full content; never sent to the browser except as `seq` |

Kind mapping:

| Source record | Claude | Oh My Pi |
|---|---|---|
| human prompt | `user` with string content not matching a wrapper tag, or `text` blocks, `isMeta` absent | `message.role=user` `text` |
| assistant text | `assistant` `text` block | `assistant` `text` |
| thinking | `assistant` `thinking` block; `preview` is `""` and a `redacted: true` flag is set when the text is empty | `assistant` `thinking` |
| tool call | `assistant` `tool_use` | `assistant` `toolCall` |
| tool result | `user` `tool_result` block | `message.role=toolResult` |
| skill | `tool_use` `name=Skill`; skill slash command; `isMeta` skill body is attached to the same `toolCallId` as `meta` | `read` with `skill://` path |
| notification | string starting `<task-notification>`; coordinator messages | `custom_message` `irc:incoming` |
| system | `system` records; `local_command` | `session_init`, `model_change`, `thinking_level_change`, `custom` `session_exit` |
| compaction | `system` `compact_boundary`; `isCompactSummary` user | none observed |
| error | `isApiErrorMessage`; `system` with `level=error` | assistant `stopReason=aborted` or `errorMessage` |
| meta (hidden by default) | `attachment`, `queue-operation`, `file-history-*`, title and mode records | `title`, `title_change`, `custom` `tool_execution_start` |

### 5.5 ToolCall

| Field | Type | Claude | Oh My Pi |
|---|---|---|---|
| `id` | str | `tool_use.id` | `toolCall.id` |
| `agentKey` | str | owner | owner |
| `name` | str | `tool_use.name` | `toolCall.name` |
| `input` | object | `tool_use.input` | `toolCall.arguments` |
| `intent` | str or null | `input.description` when present | `toolCall.intent` |
| `startedAt` | ts | record timestamp | `tool_execution_start.data.startedAt`, else record timestamp |
| `endedAt` | ts or null | timestamp of the matching `tool_result` | timestamp of the matching `toolResult` |
| `durationMs` | int or null | `endedAt - startedAt` | same |
| `status` | enum | `pending`, `ok`, `error` | same |
| `callSeq` / `resultSeq` | int or null | Event seqs | same |
| `spawnedAgentKey` | str or null | for `Agent` calls | for `task` calls |

A tool call with no result is `pending` while the agent is `running`, and stays `pending` forever if the agent ends without one.

### 5.6 SkillInvocation

| Field | Type | Claude | Oh My Pi |
|---|---|---|---|
| `skill` | str | `input.skill`, slash command name without `/`, `forked-skill.json.skillName`, or the `<command-name>` of a preloaded skill | text after `skill://` |
| `via` | enum | `tool`, `slash`, `fork`, `preload` (an `isMeta` user record with no `sourceToolUseID` whose text starts with `<command-name>` and `<skill-format>true</skill-format>`, injected for an agent definition's `skills:`) | `read` |
| `agentKey` | str | owner | owner |
| `timestamp` | ts | record timestamp | record timestamp |
| `toolCallId` | str or null | Skill `tool_use.id`; null for `slash` and `preload` | `read` `toolCall.id` |
| `args` | str or null | `input.args` or `<command-args>` | `null` |

A slash command counts as a skill only when its name is not in the built-in list `BUILTIN_SLASH_COMMANDS` in `claude_parser.py` (initial value: `clear, compact, model, login, logout, rename, resume, help, config, cost, status, exit, init, memory, permissions, agents, mcp, hooks, ide, doctor, fast, effort, plan, add-dir, context, export, list-agents`).
Built-in slash commands are counted separately under `slashCommands`.

### 5.7 Usage

One Usage per API response.

| Field | Claude | Oh My Pi |
|---|---|---|
| `messageId` | `message.id` (dedup key) | record `id` |
| `model` | `message.model` | `message.model` |
| `timestamp` | first line of that `message.id` | record timestamp |
| `inputTokens` | `input_tokens` | `input` |
| `outputTokens` | `output_tokens` | `output` |
| `cacheReadTokens` | `cache_read_input_tokens` | `cacheRead` |
| `cacheCreationTokens` | `cache_creation_input_tokens` | `cacheWrite` |
| `reasoningTokens` | `output_tokens_details.thinking_tokens` | `reasoningTokens` |
| `costUsd` | `null` | `cost.total` |
| `durationMs` | `null` | `duration` |

Claude records with `model == "<synthetic>"` produce no Usage.
When two lines share a `message.id` but disagree on `usage`, the last one wins (streaming may update `output_tokens`), and the parse stats count a `usageConflict`.

### 5.8 Stats

`Stats` is computed for one agent (`scope: "agent"`) or for an agent plus all descendants (`scope: "subtree"`, the session view uses the root's subtree).

```json
{
  "scope": "subtree",
  "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0, "reasoning": 0, "total": 0},
  "costUsd": null,
  "byModel": {"claude-opus-5-5": {"messages": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0}},
  "tools": {"Bash": {"calls": 0, "errors": 0, "totalDurationMs": 0}},
  "skills": {"code-review": {"count": 0, "via": {"tool": 0, "slash": 0, "fork": 0, "read": 0, "preload": 0}}},
  "slashCommands": {"model": 0},
  "subagentTypes": {"general-purpose": 0},
  "agents": {"total": 0, "maxDepth": 0, "running": 0},
  "durationMs": 0,
  "activeDurationMs": 0,
  "errors": {"toolErrors": 0, "apiErrors": 0, "aborted": 0},
  "turns": 0,
  "parse": {"lines": 0, "skipped": 0, "unknownTypes": {"foo": 0}}
}
```

- `total` is `input + output + cacheRead + cacheCreation`.
- `durationMs` is `lastActivityAt - startedAt`.
- `activeDurationMs` is the sum of Claude `system.turn_duration.durationMs`, or the sum of Oh My Pi assistant `duration`; `null` when no such records exist.
- `turns` counts assistant responses (distinct `messageId`) that end with a final stop reason (`end_turn`, `stop_sequence`, `stop`).

### 5.9 ParseStats

`{lines, parsed, skipped, skippedSamples, unknownTypes, usageConflicts, partialTail}`.
- `skipped` counts lines that are not valid UTF-8, not valid JSON, not a JSON object, or fail a required-field check.
- `skippedSamples` keeps at most 5 entries `{file, lineNo, offset, reason}`; no line content is kept.
- `unknownTypes` counts records whose `type` is not in the parser's known set; they become `meta` events, not skips.
- `partialTail` is `true` when the file currently ends without `\n`; the trailing bytes are held back, not counted as skipped.

## 6. Tree reconstruction

Implemented in `agent_viewer/tree.py` as a pure function `build_tree(nodes, links) -> Tree` with no I/O.
Parsers emit `Link` values while indexing; the store calls `build_tree` again whenever a link or node is added.

`Link = {kind, fromKey, toAgentId, toolCallId, timestamp, evidence}`, with `kind` in `spawn`, `resume`, `continued_in`, `parent_session`.

### 6.1 Claude Code

Input: all agent files of one project slug directory, indexed by `agentId` across every `<sessionId>/subagents/` folder of that directory.

1. Create the root node `claude:<S>:main` for each main file `<S>.jsonl`.
2. Create a node for each `agent-<A>.jsonl` found under `<S>/subagents/`, owned by session `S` (the folder it is in).
3. Resolve each subagent's parent, taking the first rule that applies:
   1. Sidecar has `parentAgentId = P`: parent is node `A=P`, looked up first in `S/subagents/`, then project-wide by `agentId`; `linkedBy = meta`.
   2. Sidecar exists and has no `parentAgentId`: parent is `claude:<S>:main`; `linkedBy = meta`.
   3. No sidecar, or unreadable sidecar: use the `spawn` link whose `toAgentId = A`; the parent is the agent whose file contained the spawning `tool_result`; `linkedBy = transcript`.
   4. No evidence: parent is `claude:<S>:main`; `linkedBy = orphan`, and the node carries a warning.
4. Spawn links are emitted by the parser from any `user` record containing a `tool_result` whose `tool_use_id` refers to a `tool_use` named `Agent` or `Task` in the same file, taking the child id from, in order: `toolUseResult.agentId`, the regex `agentId: ([0-9a-f]{8,})` over the result text.
5. Resume links are emitted from `tool_result`s of `SendMessage` tool calls, taking the id from `toolUseResult.resumedAgentId`, else the regex `"resumedAgentId"\s*:\s*"([0-9a-f]{8,})"` over the result text.
   A resume never reparents a node; it appends a Resume to the target's `resumes` and is shown as a dashed edge in the tree view.
6. When rules 3.1 or 3.2 decide the parent and a spawn link points elsewhere, the sidecar wins and the node carries the warning `parent_mismatch`.
7. If a sidecar or spawn link names a parent `P` with no file, create a placeholder node `claude:<S>:<P>` with `missing: true`, parented by rule 3.4, and attach the child to it.
8. Break cycles: walk up from each node with a visited set; on revisiting a node, reparent the node where the cycle was detected to `claude:<S>:main` and add the warning `cycle`.
9. Continuations: a `continued-in` record links session `S1` to `S2`; both stay separate sessions with `continuedIn`/`continuedFrom` set.
   A subagent file stays in the session whose folder holds it, even if its spawn link was found in `S2`; its node records `spawnSeenIn: "claude:<S2>:main"`.
10. `depth` is the number of edges to the root; the sidecar `spawnDepth` is used only as a cross-check and a mismatch adds the warning `depth_mismatch`.
11. Children are ordered by `spawnedAt`, then `agentId`, so parallel spawns from one assistant message (several `Agent` blocks sharing a `message.id`) appear as siblings in call order.

### 6.2 Oh My Pi

1. A root is any `*.jsonl` directly inside a project directory whose `session` record has no `parentSession`.
2. Every deeper `*.jsonl` (recursive) is a child; its parent is the file named by `session.parentSession` (`linkedBy = parentSession`).
3. If `parentSession` is absent or does not resolve, the parent is the file `<dir>.jsonl` next to the child's folder (`linkedBy = folder`); if that does not exist, the child hangs from the root of its top-level folder (`linkedBy = orphan`).
4. The owning session is the root reached by walking up; cycles are broken as in 6.1 step 8.
5. `agentType` and `spawnToolCallId` come from the parent's `task` tool result whose `details.progress[].id` equals the child stem; when absent they are `null`.
6. Oh My Pi has no resume concept observed; `irc:incoming` messages are shown as `notification` events in the receiving agent.

### 6.3 Missing, truncated and malformed files

- A file that disappears is kept in memory with `missing: true` and its last known content; it is never recreated or touched.
- A file whose size shrinks, or whose inode changes, is reindexed from offset 0 and the store publishes `agent.reset` (section 7.4).
- A final line without `\n` is held back until completed; if it is still incomplete when the agent is `finished`, it is counted in `skipped` with reason `truncated_tail`.
- A line that fails to parse is skipped and counted with its line number and byte offset; parsing continues with the next line.
- A sidecar that fails to parse is treated as absent and counted in the session's `parse.skipped` with reason `bad_sidecar`.
- An unknown record `type` becomes a `meta` event and increments `unknownTypes`.

## 7. Live updates

### 7.1 Discovery and watching

Polling, implemented with the standard library only, because it has no platform dependency, works identically on Linux and macOS, and the file counts are small (observed: 31 Claude project folders, 157 subagent files, 9 Oh My Pi files).

- `scan` every `SCAN_INTERVAL = 5 s`: walk both roots with `os.scandir` to find new and removed files and sidecars.
- `poll` every `POLL_INTERVAL = 1 s`: `os.stat` each file that is `running` or `idle`, or whose mtime is within `HOT_WINDOW = 15 min`; other files are polled at the scan interval.
- On `st_size > offset`: open with `open(path, "rb")`, `seek(offset)`, read at most `MAX_READ_CHUNK = 8 MiB`, split on `b"\n"`, keep the trailing partial line in a per-file buffer, advance `offset` by the bytes consumed.
- On `st_size < offset`, or `st_ino` change: reset and reindex.
- On equal size but newer mtime: re-read the first line only (Oh My Pi title rewrite) and, for Claude, do nothing else.
- Sidecars (`*.meta.json`, `*.forked-skill.json`) are re-read whole when their mtime changes; they are small.
- All file opens are read-only (`"rb"`); no locks, no `fsync`, no temp files near the transcripts.

### 7.2 Initial indexing

- At start, discover all files and read only the first 64 KiB and the last 64 KiB of each to build a cheap session summary (title, cwd, branch, start, last activity, rough status).
- Then a single background indexer thread fully indexes files in order of descending mtime, so live and recent sessions are complete first.
- Opening a session in the UI moves its files to the front of the indexer queue.
- A session summary carries `indexed: false` until all its files are fully indexed; stats of an unindexed session are `null`.

### 7.3 Status rules

Implemented in `agent_viewer/status.py` as a pure function of `(lastConversationalRecord, pendingToolCalls, explicitEnd, mtime, now, config)`.

Agent statuses: `running`, `idle`, `finished`, `stale`.

| Condition (first match wins) | Status |
|---|---|
| Oh My Pi: last non-meta record is `custom` `session_exit` | `finished` |
| Claude subagent: a `<task-notification>` with this `<task-id>` and `<status>` not `running` was seen in any file of the session | `finished` |
| Claude subagent: meta `stoppedByUser` | `finished` |
| Last conversational record is an assistant response with a final stop reason and no pending tool call | subagent: `finished`; main: `idle` if `now - mtime < IDLE_WINDOW`, else `finished` |
| Mid-turn (last record is a user prompt, a tool result, or an assistant response with pending tool calls) and `now - mtime < STALE_AFTER` | `running` |
| Mid-turn and `now - mtime >= STALE_AFTER` | `stale` |
| No conversational record yet and `now - mtime < STALE_AFTER` | `running` |
| Otherwise | `finished` |

Defaults: `IDLE_WINDOW = 30 min`, `STALE_AFTER = 10 min`; both are CLI flags.
A session's status is `running` if any agent in it is `running`, else `idle` if the root is `idle`, else `stale` if any agent is `stale`, else `finished`.
"Recently finished" in the UI means `finished` with `lastActivityAt` within `RECENT_WINDOW = 24 h` (the default list filter).
`stale` exists because Claude Code writes no end-of-session marker; a killed CLI looks mid-turn forever.

### 7.4 Server-Sent Events

Endpoint `GET /api/stream`, `Content-Type: text/event-stream`, one long-lived response per browser tab.
Query parameters narrow the stream: `session=<source>:<sessionId>` (repeatable) adds agent-level events for those sessions; without it only list-level events are sent.
`graph` (`1`, `true`, `0`, `false`; default off; any other value is `400 bad_request`) opts in to `graph.update` for the subscribed sessions; without any `session` it has no effect.

Events (`event:` name, `data:` JSON, `id:` monotonic server counter):

| Event | Data | When |
|---|---|---|
| `hello` | `{"serverTime": ts, "version": "1"}` | on connect |
| `session.upsert` | Session summary (9.2) | new session, or any summary field changed |
| `session.remove` | `{"key"}` | never in v1 (files that vanish stay with `missing`), reserved |
| `tree.update` | `{"sessionKey", "tree"}` (9.4) | node added, reparented, or status changed |
| `events.append` | `{"agentKey", "fromSeq", "toSeq", "events": [Event...]}` | new events; at most 100 events inline, else `events: []` and the client fetches the range |
| `agent.reset` | `{"agentKey"}` | file reindexed; the client drops cached events |
| `stats.update` | `{"key", "scope", "stats"}` | stats changed, coalesced to at most one per key per 2 s |
| `graph.update` | `{"sessionKey", "rev", "graph": Graph}` or `{"sessionKey", "rev", "graph": null, "overflow": true}` | the built graph of a subscribed session changed (8.5); coalesced 250 ms, at most 1 per session per 1 s, `overflow` above 512 KiB serialized; only to clients with `graph=1` |
| `heartbeat` | `{}` | every 15 s, as an SSE comment `: hb` |

- Changes are coalesced per file for `SSE_COALESCE = 250 ms` before sending.
- Each client has a bounded queue of 1,000 messages; on overflow the server sends `event: resync` and closes the stream, and the client refetches state and reconnects.
- `Last-Event-ID` is not replayed in v1; on reconnect the client refetches the visible state.
- `retry: 2000` is sent in `hello`.

## 8. UI

A single page served from `/`, with hash routing so the back button works and URLs can be bookmarked.

Routes:
- `#/` session list.
- `#/s/<source>/<sessionId>` session view: tree, timeline of the selected agent (root by default), stats.
- `#/s/<source>/<sessionId>/a/<agentId>` same view with an agent selected.

Session routes take an optional query for the graph layout (8.5):

```
#/s/<source>/<sessionId>[/a/<agentId>][?layout=graph][&tools=none|all][&drawer=timeline|stats][&tool=<name>]
```

`layout` is `tree` (default) or `graph`, `tools` is `none`, `selected` (default) or `all`, `drawer` is absent (closed), `timeline` or `stats`, and `tool` (at most 200 characters) is honored only with `layout=graph` and `drawer=timeline`.
Invalid values fall back to the defaults, keys are written in the fixed order `layout`, `tools`, `drawer`, `tool`, and defaults are omitted.

### 8.1 Session list

- Columns: status dot, source badge, project (last two path parts of `cwd`), branch, title, started, last activity (relative, updating), duration, agents, total tokens, errors.
- Filters (all in the URL query): source (`claude`, `omp`, both), project (select from `/api/projects`), branch (substring), date range (`since`, `until`), status (`running`, `idle`, `stale`, `finished`, `recent`), free text over title and first prompt.
- Default filter: running, idle and stale sessions plus finished within 24 h, sorted by last activity descending.
- Rows update in place from `session.upsert`; a new running session is inserted at its sorted position with a brief highlight.
- Pagination: 50 rows per page with a "load more" button (cursor-based).

### 8.2 Tree view

- Left panel, indented collapsible tree; the root is labelled `main`.
- Each node shows: status dot, `agentType`, description (truncated to 60 chars), short `agentId` (first 8 chars, full on hover and copy button), models, tokens, and a resume count badge.
- The `sessionId` is shown once in the panel header with a copy button.
- Warnings (`orphan`, `parent_mismatch`, `cycle`, `missing`) show as a small marker with the reason on hover.
- Resume edges are listed under the node as "resumed by <agent> at <time>".
- Clicking a node selects it and loads its timeline; `Up`/`Down` move selection, `Left`/`Right` collapse and expand.

### 8.3 Timeline (session or agent detail)

- One row per Event, newest at the bottom, virtualized by pages of 200 events.
- Row styles by kind: prompt (highlighted), text, thinking (collapsed by default; "redacted" label when empty), tool call, tool result, skill, notification, system, error, compaction divider.
- A tool call and its result render as one collapsible block: header `toolName`, intent or first input field, status, duration; body shows the input as pretty-printed JSON and the result preview.
- A `Skill` call shows the skill name in the header; an `Agent`/`task` call shows a link that selects the spawned agent in the tree.
- A truncated preview shows "Show full (N KB)", which fetches `/api/agents/.../events/<seq>`; bodies over 1 MB are fetched but shown in a scrollable `pre` with a "copy" action.
- Toggles: hide meta events (default on), hide thinking, show only errors.
- Live: while the agent is `running`, appended events animate in and the view auto-scrolls if the user is already at the bottom; otherwise a "N new events" pill appears.
- Opening an agent loads the last page first, and older pages load on scroll up.

### 8.4 Stats panel

- Right panel (below the timeline on narrow screens) for the selected node, with a toggle between `agent` and `subtree` scope.
- Blocks: token totals (4 categories, reasoning as a sub-line), tokens by model (table), tool calls (sorted table with calls, errors, total duration), skills (table with `via`), subagent types spawned (table), slash commands, duration and active duration, errors, parse stats (lines, skipped, unknown types).
- Tables only; no chart library in v1.

### 8.5 Graph canvas

An alternative "Graph" layout of the session view shows the session's agents, its continuation chain, and per-agent tool and skill usage as cards and edges on a pannable, zoomable canvas, updated live from `graph.update`.
It is specified in full in `docs/FEATURE-graph-canvas.md`, which is normative for the graph layout, the `/graph` endpoint (9.1), the `graph.update` event (7.4) and the `toolName` filter on `/events` (9.1).

## 9. HTTP API

All endpoints are `GET` (and `HEAD`), return `application/json; charset=utf-8` unless stated, and use this error envelope with a matching status code:

```json
{"error": {"code": "not_found", "message": "session claude:abc not found"}}
```

Codes: `bad_request` (400), `not_found` (404), `method_not_allowed` (405), `forbidden_host` (403), `not_ready` (409, stats of an unindexed session).

### 9.1 Endpoints

| Method and path | Returns |
|---|---|
| `GET /` and `GET /static/<file>` | frontend files from `agent_viewer/static/`, allowlisted by name |
| `GET /api/health` | `{"ok": true, "version": "1", "roots": {"claude": str, "omp": str}, "indexing": {"queued": int, "done": int}}` |
| `GET /api/projects` | `{"items": [{"source", "project", "cwd", "sessionCount"}]}` |
| `GET /api/sessions` | `{"items": [Session], "nextCursor": str or null, "total": int}` |
| `GET /api/sessions/{source}/{sessionId}` | Session with `tree` (9.4) and `stats` (subtree scope, or `null` if not indexed) |
| `GET /api/sessions/{source}/{sessionId}/tree` | Tree (9.4) |
| `GET /api/sessions/{source}/{sessionId}/graph` | Graph (`docs/FEATURE-graph-canvas.md` 8.2, example `contract/examples/graph.json`); 200 with `indexed: false` while indexing, never `not_ready` |
| `GET /api/agents/{source}/{sessionId}/{agentId}` | Agent (5.3) with `toolCalls` count, `skills` list |
| `GET /api/agents/{source}/{sessionId}/{agentId}/events` | `{"agentKey", "items": [Event], "fromSeq", "toSeq", "total", "hasBefore", "hasAfter"}` |
| `GET /api/agents/{source}/{sessionId}/{agentId}/events/{seq}` | `{"seq", "kind", "content": str, "input": object or null, "blocks": [...] }` full untruncated content |
| `GET /api/agents/{source}/{sessionId}/{agentId}/stats?scope=agent\|subtree` | Stats (5.8) |
| `GET /api/stream` | SSE (7.4) |

`/api/sessions` query parameters:
`source` (`claude`, `omp`), `project` (exact), `branch` (substring, case-insensitive), `since` and `until` (ISO date or datetime, applied to `lastActivityAt`), `status` (comma list of `running,idle,stale,finished`, or `recent`, meaning every running, idle or stale session plus those finished within the recent window), `q` (substring over title, first prompt, sessionId), `limit` (default 50, max 200), `cursor` (opaque).
Sort is `lastActivityAt` descending, ties by key.

`/events` query parameters:
`before=<seq>` or `after=<seq>` (exclusive; neither means the last page), `limit` (default 200, max 1000), `kinds` (comma list), `includeMeta` (default `false`), `toolName` (keep only events whose `toolName` equals it exactly, case-sensitive; empty means no filter; over 200 characters is `400 bad_request`).
All filters combine by AND and apply before paging, so `fromSeq`, `toSeq`, `hasBefore` and `hasAfter` refer to the filtered sequence; `total` is always the unfiltered event count of the agent; an unknown tool name gives an empty page with `null` seqs.

### 9.2 Session JSON (list item)

```json
{
  "key": "claude:e5f6a7b8-0000-4000-8000-000000000002",
  "source": "claude",
  "sessionId": "e5f6a7b8-0000-4000-8000-000000000002",
  "project": "-home-alice-src-my-project",
  "cwd": "/home/alice/src/my-project",
  "gitBranch": "feature/PROJ-123-add-search",
  "title": "Nested subagent watcher test",
  "firstPrompt": "This is a simple test...",
  "startedAt": "2026-09-25T15:38:28.779Z",
  "lastActivityAt": "2026-09-25T15:52:10.001Z",
  "status": "finished",
  "indexed": true,
  "agentCount": 9,
  "runningAgents": 0,
  "tokens": {"input": 120, "output": 5400, "cacheRead": 910000, "cacheCreation": 64000, "reasoning": 800, "total": 979520},
  "errors": 5,
  "continuedFrom": "claude:a1b2c3d4-0000-4000-8000-000000000001",
  "continuedIn": null,
  "prUrl": null
}
```

`tokens` and `errors` are `null` while `indexed` is `false`.

### 9.3 Event JSON

```json
{
  "seq": 42,
  "kind": "tool_call",
  "timestamp": "2026-09-25T15:37:09.670Z",
  "role": "assistant",
  "uuid": "b1e2...",
  "parentUuid": "a0c1...",
  "messageId": "msg_011CfQQa1N8wDrXApv5qmWeF",
  "model": "claude-opus-5-5",
  "preview": "{\"description\": \"Compute 1+99\", \"subagent_type\": \"general-purpose\", \"prompt\": \"...\"}",
  "truncated": false,
  "redacted": false,
  "toolCallId": "toolu_01ExampleToolUseId0",
  "toolName": "Agent",
  "isError": false,
  "spawnedAgentKey": "claude:a1b2c3d4-0000-4000-8000-000000000001:a13579bdf02468ace",
  "resultSeq": 43,
  "durationMs": 777
}
```

`resultSeq` and `durationMs` appear only on `tool_call` events.

### 9.4 Tree JSON

```json
{
  "sessionKey": "claude:e5f6a7b8-0000-4000-8000-000000000002",
  "rootKey": "claude:e5f6a7b8-0000-4000-8000-000000000002:main",
  "nodes": {
    "claude:e5f6...:main": {"key": "...", "agentId": "main", "parentKey": null, "depth": 0, "agentType": "main", "status": "idle", "children": ["claude:e5f6...:afedcba9876543210"], "linkedBy": "root", "warnings": [], "resumes": [], "models": ["claude-opus-5-5"], "tokensTotal": 900000, "eventCount": 310},
    "claude:e5f6...:afedcba9876543210": {"key": "...", "agentId": "afedcba9876543210", "parentKey": "claude:e5f6...:main", "depth": 1, "agentType": "general-purpose", "description": "Watcher live test parent", "status": "finished", "children": ["claude:e5f6...:a2468ace13579bdf0"], "linkedBy": "meta", "warnings": [], "resumes": [], "models": ["claude-haiku-4-5-20251001"], "tokensTotal": 40000, "eventCount": 22}
  }
}
```

Nodes are a flat map keyed by agent key; `children` gives order.
Tree node objects are Agent (5.3) minus `file`, plus `warnings` and `tokensTotal`.

### 9.5 Full event JSON

```json
{"seq": 43, "kind": "tool_result", "toolCallId": "toolu_01Cv...", "isError": false, "content": "Async agent launched successfully.\nagentId: a13579bdf02468ace ...", "input": null, "blocks": [{"type": "text", "text": "..."}]}
```

Image blocks are replaced by `{"type": "image", "mediaType": "image/png", "bytes": 12345}`; image data is never sent.

## 10. Tech stack

- Backend: Python 3 standard library only, run with the system `python3` (3.9 or newer; 3.11.5 is installed), not Poetry and not a virtualenv.
  - `http.server.ThreadingHTTPServer` with a small router, `json`, `threading`, `queue`, `os`, `re`, `pathlib`, `argparse`, `unittest`.
- Frontend: one `index.html`, plain ES modules (`type="module"`), one CSS file, `EventSource` for SSE, `fetch` for the API.
  No build step, no npm packages, no CDN.
- Dev-only: `node --test` (Node 18 or newer; Node 24.8.0 is installed) for the frontend's pure modules; Playwright for Python for the optional browser smoke test, skipped when not importable.

Justification:
- The workload is a handful of concurrent local clients, file polling and JSON; the stdlib handles it, so FastAPI, uvicorn and pydantic would add a virtualenv and upgrade churn for no functional gain (FastAPI is not installed on the system interpreter).
- SSE over `http.server` needs only a response that is never closed plus a per-client queue; no async framework is required.
- Polling avoids `inotify`/`watchdog` dependencies and edge cases with editors and rotated files, at a cost of a few hundred `stat` calls per second at most.
- A no-CDN frontend works offline, allows a strict `script-src 'self'` CSP, and has no supply chain.
- Vanilla DOM with `textContent` is the simplest way to guarantee no transcript content is ever interpreted as HTML.
- Many local developer tools already run on system `python3` with the stdlib, so this adds no new runtime.

Run: `python3 -m agent_viewer [--port 8765] [--claude-root ~/.claude/projects] [--omp-root ~/.omp/agent/sessions] [--idle-window 30m] [--stale-after 10m] [--open]`.
The host is fixed to `127.0.0.1` and has no flag.

## 11. Security

- Bind to `127.0.0.1` only; `0.0.0.0`, `::` and LAN addresses are not configurable.
- Reject any request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` with 403 `forbidden_host`, to block DNS rebinding.
- Only `GET` and `HEAD` are accepted; every other method returns 405.
- No endpoint accepts a file path; files are addressed only by keys that map to paths discovered under the two roots.
  Static files are served from a fixed allowlist, so `..` and absolute paths cannot reach the filesystem.
- Every discovered path is checked with `os.path.realpath` to be under its root before opening; symlinks that escape the root are skipped and counted.
- Transcript files are opened with mode `"rb"` only; the codebase contains no call that writes, renames, truncates or deletes under either root, enforced by a test that greps the package for `open(` modes other than `"rb"`/`"r"` and for `os.remove`, `os.rename`, `shutil`, `unlink`, `write_text`.
- Response headers on every response:
  `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for `/api/*`.
- No CORS headers are sent, so other origins cannot read responses.
- Transcript content is untrusted: the frontend inserts it only with `textContent`, `createTextNode` or attribute setters on non-URL attributes; `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function` and `javascript:` URLs are banned and checked by a test that greps `static/`.
- URLs inside transcript content are shown as text, never as clickable links, in v1.
- The server never executes transcript content, never runs shell commands, and makes no outbound network requests.
- Transcripts can contain secrets; the page is only reachable from the local machine and logs contain keys and counts, never content.

## 12. Performance

Observed sizes: largest main transcript 28 MB, largest subagent transcript 22 MB.

- Index, do not hold: per Event the store keeps a compact record (`seq`, kind, timestamp, ids, a 2,000-character preview, and the `ref` offset/length), not the full JSON.
- Full content is served by seeking to `ref.offset` and re-parsing that single line.
- Budgets on this machine, measured by `tests/perf/test_index_budget.py` against a generated 30 MB fixture:
  - full index of a 30 MB file in under 3 s;
  - resident memory for that file under 150 MB;
  - an `events` page of 200 in under 50 ms;
  - an appended line visible in the browser in under 2 s end to end.
- Preview truncation at 2,000 characters and `limit` caps (200 default, 1000 max) bound response sizes; tool inputs in list events are serialized and truncated the same way.
- Stats are maintained incrementally as events are indexed, so a stats request is a dictionary merge over the subtree, not a rescan.
- SSE coalescing (250 ms) and the per-client queue cap protect the browser during bursts.
- Frontend: render only the loaded pages, collapse tool bodies by default, and lazy-load full content.

## 13. Testing

- Framework: `unittest` from the stdlib; run with `python3 -m unittest discover -s tests`.
- Frontend pure modules: `node --test agent_viewer/static/tests/`.
- Coverage target: 80% of lines in `agent_viewer/` measured with `coverage` when available (optional dev tool, not a runtime dependency).
- Every test uses fixture roots under `tests/fixtures/`, never the real home directory.

### 13.1 Fixture layout

```
tests/fixtures/
  claude/
    -tmp-demo/
      s-main.jsonl
      s-main/subagents/agent-a1111111111111111.jsonl
      s-main/subagents/agent-a1111111111111111.meta.json
      s-main/subagents/agent-a2222222222222222.jsonl
      s-main/subagents/agent-a2222222222222222.meta.json
      s-main/subagents/agent-a3333333333333333.jsonl          no sidecar, linked by transcript
      s-main/subagents/agent-a4444444444444444.jsonl          no sidecar, no evidence, orphan
    -tmp-broken/
      s-broken.jsonl                                           bad lines and truncated tail
  omp/
    -tmp-demo/
      2026-09-22T09-58-35-301Z_o-root.jsonl
      2026-09-22T09-58-35-301Z_o-root/Checker.jsonl
```

### 13.2 Fixture contents

Lines are shown wrapped only for readability of this spec; each record is one line in the file.
Fields irrelevant to a test (`entrypoint`, `userType`, `version`) may be omitted from fixtures, which also exercises tolerance of missing fields.

`claude/-tmp-demo/s-main.jsonl`:

```jsonl
{"type":"custom-title","customTitle":"Demo session","sessionId":"s-main"}
{"type":"user","uuid":"u1","parentUuid":null,"sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:00.000Z","message":{"role":"user","content":"Spawn two helpers and use the review skill. <img src=x onerror=alert(1)>"}}
{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:01.000Z","message":{"id":"msg_1","model":"claude-opus-5-5","stop_reason":"tool_use","content":[{"type":"thinking","thinking":"","signature":"x"}],"usage":{"input_tokens":10,"output_tokens":50,"cache_read_input_tokens":1000,"cache_creation_input_tokens":200}}}
{"type":"assistant","uuid":"a2","parentUuid":"a1","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:01.100Z","message":{"id":"msg_1","model":"claude-opus-5-5","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_A","name":"Agent","input":{"description":"Helper one","subagent_type":"general-purpose","prompt":"Do one"}}],"usage":{"input_tokens":10,"output_tokens":50,"cache_read_input_tokens":1000,"cache_creation_input_tokens":200}}}
{"type":"assistant","uuid":"a3","parentUuid":"a2","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:01.200Z","message":{"id":"msg_1","model":"claude-opus-5-5","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_B","name":"Agent","input":{"description":"Helper legacy","subagent_type":"ecc:code-explorer","prompt":"Do two"}}],"usage":{"input_tokens":10,"output_tokens":50,"cache_read_input_tokens":1000,"cache_creation_input_tokens":200}}}
{"type":"user","uuid":"u2","parentUuid":"a2","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_A","content":[{"type":"text","text":"Async agent launched successfully.\nagentId: a1111111111111111 (internal ID)"}]}]},"toolUseResult":{"isAsync":true,"status":"async_launched","agentId":"a1111111111111111","description":"Helper one"}}
{"type":"user","uuid":"u3","parentUuid":"a3","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.100Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_B","content":"Async agent launched successfully.\nagentId: a3333333333333333 (internal ID)"}]}}
{"type":"assistant","uuid":"a4","parentUuid":"u3","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.000Z","message":{"id":"msg_2","model":"claude-opus-5-5","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_S","name":"Skill","input":{"skill":"code-review","args":"low"}}],"usage":{"input_tokens":5,"output_tokens":20,"cache_read_input_tokens":1200,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"u4","parentUuid":"a4","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.500Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_S","content":"Launching skill: code-review"}]},"toolUseResult":{"success":true,"commandName":"code-review"}}
{"type":"assistant","uuid":"a5","parentUuid":"u4","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:04.000Z","message":{"id":"msg_3","model":"claude-opus-5-5","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_M","name":"SendMessage","input":{"to":"a1111111111111111","message":"Try again"}}],"usage":{"input_tokens":5,"output_tokens":10,"cache_read_input_tokens":1300,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"u5","parentUuid":"a5","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:04.500Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_M","content":[{"type":"text","text":"{\"success\":true,\"resumedAgentId\":\"a1111111111111111\"}"}]}]},"toolUseResult":{"success":true,"resumedAgentId":"a1111111111111111"}}
{"type":"user","uuid":"u6","parentUuid":"u5","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:30.000Z","message":{"role":"user","content":"<task-notification>\n<task-id>a1111111111111111</task-id>\n<tool-use-id>toolu_A</tool-use-id>\n<status>completed</status>\n</task-notification>"}}
{"type":"user","uuid":"u7","parentUuid":"u6","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:31.000Z","message":{"role":"user","content":"<command-name>/model</command-name><command-message>model</command-message><command-args>haiku</command-args>"}}
{"type":"assistant","uuid":"a6","parentUuid":"u7","sessionId":"s-main","isSidechain":false,"cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:32.000Z","message":{"id":"msg_4","model":"claude-opus-5-5","stop_reason":"end_turn","content":[{"type":"text","text":"Done. <script>alert(1)</script>"}],"usage":{"input_tokens":5,"output_tokens":30,"cache_read_input_tokens":1400,"cache_creation_input_tokens":0}}}
{"type":"system","subtype":"turn_duration","durationMs":32000,"uuid":"y1","parentUuid":"a6","sessionId":"s-main","timestamp":"2026-09-25T10:00:32.100Z"}
{"type":"brand-new-type","sessionId":"s-main"}
```

Expected from `s-main.jsonl` alone: Usage for 4 distinct message ids (`msg_1` counted once: input 10, output 50, cacheRead 1000, cacheCreation 200); totals input 25, output 110, cacheRead 4900, cacheCreation 200; tools `Agent: 2, Skill: 1, SendMessage: 1`; skill `code-review` via `tool`; slash command `model` counted, not a skill; one `unknownTypes["brand-new-type"]`; one `resume` link to `a1111111111111111`; spawn links to `a1111111111111111` and `a3333333333333333`; the thinking event is `redacted`; `activeDurationMs` 32000.

`s-main/subagents/agent-a1111111111111111.meta.json`:

```json
{"agentType":"general-purpose","description":"Helper one","toolUseId":"toolu_A","spawnDepth":1,"requestShape":"background","requestNonInteractive":true}
```

`s-main/subagents/agent-a1111111111111111.jsonl` (spawns a nested child, then is resumed):

```jsonl
{"type":"user","uuid":"c1","parentUuid":null,"isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.050Z","message":{"role":"user","content":"Do one"}}
{"type":"assistant","uuid":"c2","parentUuid":"c1","isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.500Z","message":{"id":"msg_c1","model":"claude-haiku-4-5-20251001","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_N","name":"Agent","input":{"description":"Nested child","subagent_type":"general-purpose","prompt":"Leaf"}}],"usage":{"input_tokens":3,"output_tokens":7,"cache_read_input_tokens":0,"cache_creation_input_tokens":500}}}
{"type":"user","uuid":"c3","parentUuid":"c2","isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.700Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_N","content":"agentId: a2222222222222222"}]},"toolUseResult":{"agentId":"a2222222222222222","status":"async_launched"}}
{"type":"assistant","uuid":"c4","parentUuid":"c3","isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.000Z","message":{"id":"msg_c2","model":"claude-haiku-4-5-20251001","stop_reason":"end_turn","content":[{"type":"text","text":"100"}],"usage":{"input_tokens":3,"output_tokens":2,"cache_read_input_tokens":500,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"c5","parentUuid":"c4","isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:04.600Z","message":{"role":"user","content":"The coordinator sent a message while you were working:\nTry again"}}
{"type":"assistant","uuid":"c6","parentUuid":"c5","isSidechain":true,"agentId":"a1111111111111111","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:05.000Z","message":{"id":"msg_c3","model":"claude-haiku-4-5-20251001","stop_reason":"end_turn","content":[{"type":"text","text":"Still 100"}],"usage":{"input_tokens":3,"output_tokens":3,"cache_read_input_tokens":500,"cache_creation_input_tokens":0}}}
```

`s-main/subagents/agent-a2222222222222222.meta.json`:

```json
{"agentType":"general-purpose","description":"Nested child","toolUseId":"toolu_N","parentAgentId":"a1111111111111111","spawnDepth":2,"requestShape":"background","requestNonInteractive":true}
```

`s-main/subagents/agent-a2222222222222222.jsonl`:

```jsonl
{"type":"user","uuid":"d1","parentUuid":null,"isSidechain":true,"agentId":"a2222222222222222","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:02.800Z","message":{"role":"user","content":"Leaf"}}
{"type":"assistant","uuid":"d2","parentUuid":"d1","isSidechain":true,"agentId":"a2222222222222222","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.200Z","message":{"id":"msg_d1","model":"claude-haiku-4-5-20251001","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_X","name":"Bash","input":{"command":"false"}}],"usage":{"input_tokens":2,"output_tokens":4,"cache_read_input_tokens":0,"cache_creation_input_tokens":300}}}
{"type":"user","uuid":"d3","parentUuid":"d2","isSidechain":true,"agentId":"a2222222222222222","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.400Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_X","content":"Exit code 1","is_error":true}]}}
{"type":"assistant","uuid":"d4","parentUuid":"d3","isSidechain":true,"agentId":"a2222222222222222","sessionId":"s-main","cwd":"/tmp/demo","gitBranch":"feature/DEMO-1","timestamp":"2026-09-25T10:00:03.600Z","message":{"id":"msg_d2","model":"claude-haiku-4-5-20251001","stop_reason":"end_turn","content":[{"type":"text","text":"Leaf done"}],"usage":{"input_tokens":2,"output_tokens":2,"cache_read_input_tokens":300,"cache_creation_input_tokens":0}}}
```

`s-main/subagents/agent-a3333333333333333.jsonl` (no sidecar; parent found from the `toolu_B` result in `s-main.jsonl`; last line truncated mid-write, no trailing newline):

```jsonl
{"type":"user","uuid":"e1","parentUuid":null,"isSidechain":true,"agentId":"a3333333333333333","sessionId":"s-main","cwd":"/tmp/demo","timestamp":"2026-09-25T10:00:02.150Z","message":{"role":"user","content":"Do two"}}
{"type":"assistant","uuid":"e2","parentUuid":"e1","isSidechain":true,"agentId":"a3333333333333333","sessionId":"s-main","cwd":"/tmp/demo","timestamp":"2026-09-25T10:00:02.600Z","message":{"id":"msg_e1","model":"claude-sonnet-4-6","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_R","name":"Read","input":{"file_path":"/tmp/demo/x"}}],"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"e3","parentUuid":"e2","isSidechain":true,"agentId":"a3333
```

`s-main/subagents/agent-a4444444444444444.jsonl` (no sidecar, never referenced):

```jsonl
{"type":"user","uuid":"f1","parentUuid":null,"isSidechain":true,"agentId":"a4444444444444444","sessionId":"s-main","cwd":"/tmp/demo","timestamp":"2026-09-25T10:01:00.000Z","message":{"role":"user","content":"Orphan"}}
```

Expected tree for `claude:s-main`:

```
main
  a1111111111111111  general-purpose  linkedBy=meta        resumes=1  status=finished (task-notification)
    a2222222222222222  general-purpose  linkedBy=meta      depth=2    tools: Bash 1 (1 error)
  a3333333333333333  ecc:code-explorer  linkedBy=transcript  partialTail=true  status=running or stale (by mtime)
  a4444444444444444  agentType=null  linkedBy=orphan  warnings=[orphan]
```

`agentType` of `a3333333333333333` comes from the spawning call's `input.subagent_type` because it has no sidecar; the parser records it on the spawn link.

`claude/-tmp-broken/s-broken.jsonl` (6 lines shown, the last one without a trailing newline):

```jsonl
{"type":"user","uuid":"g1","parentUuid":null,"sessionId":"s-broken","cwd":"/tmp/broken","timestamp":"2026-09-25T11:00:00.000Z","message":{"role":"user","content":"hello"}}
this is not json
[1,2,3]
{"type":"assistant","uuid":"g2","parentUuid":"g1","sessionId":"s-broken","timestamp":"2026-09-25T11:00:01.000Z","message":{"id":"msg_g","model":"<synthetic>","stop_reason":"end_turn","content":[{"type":"text","text":"No response requested."}],"usage":{"input_tokens":0,"output_tokens":0}}}
{"type":"assistant","uuid":"g3","parentUuid":"g2","sessionId":"s-broken","timestamp":"2026-09-25T11:00:02.000Z","isApiErrorMessage":true,"message":{"id":"msg_h","model":"claude-opus-5-5","stop_reason":"stop_sequence","content":[{"type":"text","text":"API Error: Connection lost mid-response."}],"usage":{"input_tokens":1,"output_tokens":0}}}
{"type":"user","message":
```

The file also contains one line of invalid UTF-8 bytes (`\xff\xfe`) inserted after line 3 by the fixture generator `tests/fixtures/make_binary_line.py`, because it cannot be shown here.
Expected: `lines 6` complete lines, `parsed 3`, `skipped 3` (`not_json`, `not_object`, `bad_utf8`), `partialTail true`; no Usage from `<synthetic>`; one `apiErrors`; the server stays up.

`omp/-tmp-demo/2026-09-22T09-58-35-301Z_o-root.jsonl`:

```jsonl
{"type":"title","v":1,"title":"OMP demo","source":"auto","updatedAt":"2026-09-22T10:07:16.697Z","pad":"          "}
{"type":"session","version":3,"id":"o-root","timestamp":"2026-09-22T09:58:35.301Z","cwd":"/tmp/demo","title":"OMP demo"}
{"type":"model_change","id":"m1","parentId":null,"timestamp":"2026-09-22T09:58:36.000Z","model":"anthropic/claude-opus-5","role":"default"}
{"type":"message","id":"p1","parentId":"m1","timestamp":"2026-09-22T09:58:40.000Z","message":{"role":"user","content":[{"type":"text","text":"Check the list"}],"attribution":"user","timestamp":1790000000000}}
{"type":"message","id":"p2","parentId":"p1","timestamp":"2026-09-22T09:58:45.000Z","message":{"role":"assistant","model":"gpt-5.6-sol","provider":"github-copilot","stopReason":"toolUse","duration":4000,"content":[{"type":"thinking","thinking":"Load the skill, then delegate."},{"type":"toolCall","id":"call_1|x","name":"read","arguments":{"path":"skill://jira-integration"},"intent":"Reading Jira guidance"},{"type":"toolCall","id":"call_2|y","name":"task","arguments":{"context":"Test it"},"intent":"Delegating check"}],"usage":{"input":3,"output":194,"cacheRead":0,"cacheWrite":19726,"totalTokens":19923,"reasoningTokens":119,"cost":{"total":0.1}}}}
{"type":"custom","customType":"tool_execution_start","data":{"toolCallId":"call_1|x","toolName":"read","startedAt":"2026-09-22T09:58:45.100Z","args":{"path":"skill://jira-integration"}},"id":"t1","parentId":"p2","timestamp":"2026-09-22T09:58:45.100Z"}
{"type":"message","id":"p3","parentId":"p2","timestamp":"2026-09-22T09:58:45.300Z","message":{"role":"toolResult","toolCallId":"call_1|x","toolName":"read","isError":false,"content":[{"type":"text","text":"# jira-integration ..."}],"details":{}}}
{"type":"message","id":"p4","parentId":"p3","timestamp":"2026-09-22T09:58:46.000Z","message":{"role":"toolResult","toolCallId":"call_2|y","toolName":"task","isError":false,"content":[{"type":"text","text":"Spawned agent `Checker`"}],"details":{"progress":[{"index":0,"id":"Checker","agent":"reviewer","status":"pending"}]}}}
{"type":"message","id":"p5","parentId":"p4","timestamp":"2026-09-22T10:10:00.000Z","message":{"role":"assistant","model":"gpt-5.6-sol","stopReason":"stop","duration":2000,"content":[{"type":"text","text":"All good"}],"usage":{"input":5,"output":10,"cacheRead":19726,"cacheWrite":0,"totalTokens":19741}}}
{"type":"custom","customType":"session_exit","data":{"reason":"dispose","kind":"normal","recordedAt":"2026-09-22T10:10:05.000Z"},"id":"x1","parentId":"p5","timestamp":"2026-09-22T10:10:05.000Z"}
```

`omp/-tmp-demo/2026-09-22T09-58-35-301Z_o-root/Checker.jsonl` (the `parentSession` value is written by the fixture loader as the absolute path of the root file, since fixtures move):

```jsonl
{"type":"session","version":3,"id":"o-child","timestamp":"2026-09-22T09:58:46.500Z","cwd":"/tmp/demo","parentSession":"__FIXTURE_ROOT__/omp/-tmp-demo/2026-09-22T09-58-35-301Z_o-root.jsonl"}
{"type":"session_init","id":"i1","parentId":null,"timestamp":"2026-09-22T09:58:46.600Z","systemPrompt":"You are a reviewer."}
{"type":"custom_message","customType":"irc:incoming","id":"q1","parentId":"i1","timestamp":"2026-09-22T09:59:00.000Z","content":"<irc>Incoming IRC message from agent `Main`: hurry</irc>","display":true}
{"type":"message","id":"r1","parentId":"q1","timestamp":"2026-09-22T09:59:10.000Z","message":{"role":"assistant","model":"gemini-3.8-flash","stopReason":"aborted","errorMessage":"Request was aborted","content":[],"usage":{"input":1,"output":0,"cacheRead":0,"cacheWrite":0}}}
{"type":"custom","customType":"session_exit","data":{"reason":"dispose","kind":"normal"},"id":"x2","parentId":"r1","timestamp":"2026-09-22T09:59:11.000Z"}
```

Expected for `omp:o-root`: root `main` with child `Checker` (`agentType: reviewer`, `linkedBy: parentSession`, `spawnToolCallId: call_2|y`); skill `jira-integration` via `read`; tools `read: 1, task: 1`; tokens input 9, output 204, cacheRead 19726, cacheCreation 19726; `costUsd 0.1`; one `aborted` error in `Checker`; both agents `finished`; `gitBranch null`.

The fixture loader (`tests/fixtures/loader.py`) copies `tests/fixtures/` to a temporary directory, replaces `__FIXTURE_ROOT__`, and runs `make_binary_line.py`; tests never modify the committed fixtures.

### 13.3 Test suites

| Suite | Owner | Covers |
|---|---|---|
| `tests/unit/test_claude_parser.py` | WS1 | block to Event mapping, usage dedup by `message.id`, synthetic exclusion, skill forms, slash commands, links, error detection, unknown types |
| `tests/unit/test_omp_parser.py` | WS1 | record mapping, usage mapping, `skill://`, `task` progress, `session_exit`, `irc:incoming` |
| `tests/unit/test_parse_common.py` | WS1 | bad JSON, non-object, bad UTF-8, partial tail, skipped samples cap |
| `tests/unit/test_tree.py` | WS1 | every rule and warning in 6.1 and 6.2, including cycle, missing parent placeholder, parallel spawn ordering, continuation |
| `tests/unit/test_status.py` | WS1 | every row of the table in 7.3 with an injected `now` |
| `tests/unit/test_stats.py` | WS1 | agent and subtree aggregation, expected totals from 13.2 |
| `tests/unit/test_tail.py` | WS2 | partial lines across reads, growth, shrink reset, inode change, chunk limit |
| `tests/unit/test_store.py` | WS2 | incremental apply equals full parse, SSE message generation, coalescing |
| `tests/unit/test_api.py` | WS2 | every endpoint and error code, pagination bounds, host check, headers, 405 |
| `tests/unit/test_readonly_guard.py` | WS2 | static grep for forbidden write calls in `agent_viewer/` |
| `tests/e2e/test_smoke.py` | WS2 | section 13.4 |
| `agent_viewer/static/tests/*.test.js` | WS3 | formatting, tree flattening, event grouping, URL state, the banned-API grep |
| `tests/e2e/test_ui_smoke.py` | WS3 | section 13.5, skipped if Playwright is not importable |
| `tests/perf/test_index_budget.py` | WS2 | budgets in section 12, excluded from the default run, run with `AGENT_VIEWER_PERF=1` |

### 13.4 E2E smoke test (HTTP level, required)

1. Copy fixtures to a temp dir, record SHA-256 and mtime of every file.
2. Start `python3 -m agent_viewer --port 0 --claude-root <tmp>/claude --omp-root <tmp>/omp` as a subprocess; read the chosen port from its first stdout line `listening on http://127.0.0.1:<port>`.
3. Assert `/api/health` is `ok` within 5 s and indexing reaches `queued == 0` within 10 s.
4. Assert `/api/sessions` returns 3 sessions and the trees and stats match section 13.2.
5. Open `/api/stream?session=claude:s-main`, then append one assistant line to the fixture copy of `agent-a2222222222222222.jsonl` in two writes (first half, sleep 300 ms, second half with `\n`); assert exactly one `events.append` for that agent arrives within 3 s and carries the new text.
6. Assert a request with `Host: evil.example:<port>` gets 403, and `POST /api/sessions` gets 405.
7. Stop the server and assert every fixture file except the one appended by the test has the same SHA-256 and mtime as in step 1.

### 13.5 Browser smoke test (optional)

With Playwright for Python and Chromium available: open `/`, see 3 sessions, open `claude:s-main`, see the tree of section 13.2, open `a2222222222222222`, expand the Bash call and see "Exit code 1" as an error, and assert that no `img` or `script` element exists inside the timeline container and no dialog was raised (the fixture contains `<img src=x onerror=alert(1)>` and `<script>`).

## 14. Workstreams

Three workstreams with disjoint file ownership.
A file is edited only by its owner; a change to a contract in section 14.5 needs agreement from both sides and an edit to this spec first.

### 14.1 Repository layout

```
agent-viewer/
  SPEC.md                                   shared, edited only by agreement
  README.md                                 WS2 (run instructions)
  agent_viewer/
    __init__.py                             WS1 (version string only)
    model.py                                WS1 dataclasses + to_dict (section 5)
    parse_common.py                         WS1 safe line decoding, ParseStats
    claude_parser.py                        WS1
    omp_parser.py                           WS1
    discovery.py                            WS1 find files and sidecars under the roots
    accumulate.py                           WS1 AgentState: applies ParseResults, keeps event index, tool calls, stats
    tree.py                                 WS1 build_tree (section 6)
    status.py                               WS1 decide_status (section 7.3)
    stats.py                                WS1 aggregation (section 5.8)
    tail.py                                 WS2 FileTail (byte-offset incremental reader)
    store.py                                WS2 in-memory store of sessions, AgentStates, FileTails; change feed
    watcher.py                              WS2 scan and poll threads, indexer queue
    sse.py                                  WS2 client registry, queues, framing, coalescing
    api.py                                  WS2 routing and JSON handlers (section 9)
    server.py                               WS2 HTTP server, static files, security headers, host check
    __main__.py                             WS2 CLI
    static/                                 WS3
      index.html
      style.css
      app.js                                router and bootstrapping
      api.js                                fetch and EventSource wrapper, the only module that talks to the server
      dom.js                                safe element builder (the only DOM construction helper)
      views/sessions.js
      views/tree.js
      views/timeline.js
      views/stats.js
      lib/format.js                         pure: numbers, durations, relative times, truncation
      lib/state.js                          pure: URL <-> filter state, event page merging
      tests/*.test.js
  contract/
    examples/                               WS2 owns, frozen on day 1 from section 9 (JSON files per endpoint)
    mock_server.py                          WS2, stdlib server that serves contract/examples/ with the real routes and a scripted SSE feed
  tests/
    fixtures/                               WS1 (section 13.1)
    unit/test_claude_parser.py ... test_stats.py   WS1
    unit/test_tail.py ... test_readonly_guard.py   WS2
    e2e/test_smoke.py                       WS2
    e2e/test_ui_smoke.py                    WS3
    perf/test_index_budget.py               WS2
```

### 14.2 Workstream 1: parsers, model, tree builder

Owns everything marked WS1 above.
Delivers pure, I/O-light code: the only I/O in WS1 is `discovery.py` (directory listing and sidecar reading) and the fixture loader.

### 14.3 Workstream 2: server, API, watcher, SSE

Owns everything marked WS2 above.
Starts on day 1 with `contract/examples/` and `contract/mock_server.py` so WS3 is unblocked, and with a stub WS1 interface (section 14.5.1) returning canned objects until WS1 lands.

### 14.4 Workstream 3: frontend

Owns `agent_viewer/static/` and `tests/e2e/test_ui_smoke.py`.
Develops against `python3 contract/mock_server.py --port 8766`, then against the real server with no code change (same routes and shapes).

### 14.5 Interface contracts

#### 14.5.1 WS1 -> WS2 (Python)

```python
# discovery.py
@dataclass(frozen=True)
class SourceFile:
    source: str            # "claude" | "omp"
    kind: str              # "main" | "subagent" | "meta" | "forked_skill" | "omp_root" | "omp_child"
    path: str              # absolute, realpath under its root
    project: str           # slug or project directory name
    session_hint: str      # Claude: owning sessionId from the path; Omp: root file stem
    agent_hint: str        # "main", Claude agentId, or Omp relative stem path

def discover(claude_root: str | None, omp_root: str | None) -> list[SourceFile]: ...
def read_sidecar(path: str) -> dict | None: ...          # None on any error, never raises

# parse_common.py
@dataclass
class ParseStats: ...                                    # fields of section 5.9
def decode_line(raw: bytes, line_no: int, offset: int, stats: ParseStats) -> dict | None: ...

# claude_parser.py / omp_parser.py, same shape
@dataclass
class ParseResult:
    events: list[Event]
    tool_call_updates: list[ToolCallUpdate]   # start or finish of a ToolCall
    usages: list[Usage]
    skills: list[SkillInvocation]
    links: list[Link]
    session_meta: dict                        # title, cwd, gitBranch, version, prUrl, continuedIn, ...
    status_signals: list[StatusSignal]        # final stop, pending tool call, task-notification, session_exit

class ClaudeParser:
    source = "claude"
    def __init__(self, agent_key: str): ...
    def parse_record(self, record: dict, offset: int, length: int, line_no: int) -> ParseResult: ...
    def full_content(self, record: dict, block: int) -> dict: ...   # section 9.5 body
class OmpParser: ...                                                # same methods

# accumulate.py
class AgentState:
    def __init__(self, agent_key: str, parser): ...
    def feed_line(self, raw: bytes, offset: int, line_no: int) -> ParseResult: ...   # decode + parse + apply
    def reset(self) -> None: ...
    def events(self, before: int | None, after: int | None, limit: int, kinds: set[str] | None, include_meta: bool) -> list[Event]: ...
    def event_ref(self, seq: int) -> EventRef: ...
    def stats(self) -> Stats: ...                          # scope "agent"
    parse_stats: ParseStats
    links: list[Link]
    status_signals: list[StatusSignal]

# tree.py
def build_tree(nodes: list[NodeInput], links: list[Link]) -> Tree: ...    # pure, deterministic

# status.py
def decide_status(signals: list[StatusSignal], mtime: float, now: float, is_root: bool, config: StatusConfig) -> str: ...

# stats.py
def merge(stats: list[Stats]) -> Stats: ...               # subtree scope
```

Rules: no WS1 function raises on bad input data; WS1 never opens transcript files (WS2's `tail.py` does and passes bytes in), except `read_sidecar` and `full_content` callers passing a re-read line.

#### 14.5.2 WS2 -> WS3 (HTTP)

The HTTP API of section 9 and the SSE stream of section 7.4, with the examples in `contract/examples/` as the normative instances.
File names in `contract/examples/`: `health.json`, `projects.json`, `sessions.json`, `session_detail.json`, `tree.json`, `agent.json`, `events_page.json`, `event_full.json`, `stats_agent.json`, `stats_subtree.json`, `error_not_found.json`, `sse_script.jsonl` (one `{"delayMs", "event", "data"}` per line, replayed by `mock_server.py`).
Unknown fields in any response must be ignored by the frontend, so WS2 may add fields without breaking WS3.

## 15. Acceptance criteria

### 15.1 WS1

- A1.1 All WS1 unit suites pass with `python3 -m unittest`, with at least 80% line coverage of WS1 modules.
- A1.2 Parsing every fixture in section 13.2 yields exactly the expected trees, totals, skills, tools, links and parse stats written there.
- A1.3 Usage from lines sharing a `message.id` is counted once.
- A1.4 No WS1 function raises on any input line, including random bytes (property test with 10,000 generated lines).
- A1.5 `build_tree` is deterministic: shuffling the input order of nodes and links gives an identical tree.
- A1.6 Running the parsers over every real file under `~/.claude/projects` and `~/.omp/agent/sessions` (manual check, not committed) finishes without exceptions and reports `skipped` below 0.1% of lines.
- A1.7 No imports outside the Python standard library.

### 15.2 WS2

- A2.1 `python3 -m agent_viewer` starts on system `python3` with no third-party packages and prints `listening on http://127.0.0.1:<port>`.
- A2.2 Every endpoint in section 9 returns the documented shape, validated against `contract/examples/` by `test_api.py`.
- A2.3 The e2e smoke test of section 13.4 passes, including the read-only checksum check.
- A2.4 A line appended to a watched file appears as an `events.append` SSE message within 2 s; a line written in two parts produces one event, not a skip.
- A2.5 Truncating a watched file triggers `agent.reset` and a correct reindex.
- A2.6 The server binds only to `127.0.0.1`, returns 403 for foreign `Host` headers and 405 for non-GET methods, and sends the headers of section 11.
- A2.7 `test_readonly_guard.py` passes.
- A2.8 Perf budgets of section 12 hold on the 30 MB generated fixture.
- A2.9 The server keeps running when a watched file is deleted, replaced, or contains garbage.

### 15.3 WS3

- A3.1 The UI loads with no network requests except to its own origin, and with the CSP of section 11 producing no violations in the browser console.
- A3.2 The session list shows, filters (source, project, branch, date, status, text) and live-updates rows against `mock_server.py` and the real server.
- A3.3 The tree view shows every node with `agentType`, short `agentId` with copy, status and warnings, and the `sessionId` in its header.
- A3.4 The timeline shows prompts, text, thinking (with the redacted label), tool calls paired with results in collapsible blocks, skills and notifications, and loads older pages on scroll.
- A3.5 The stats panel shows every block of section 8.4 in both scopes.
- A3.6 Transcript content containing `<script>`, `<img onerror>`, and `javascript:` text renders as literal text; the banned-API grep test passes.
- A3.7 `node --test agent_viewer/static/tests/` passes.
- A3.8 The UI survives an SSE disconnect: it shows a "reconnecting" state and resyncs when the server returns.
- A3.9 Usable at 1280x800 and at 390px width (stats panel stacks below the timeline).

## 16. Risks and open questions

- Both transcript schemas are undocumented and changed recently (for example `.meta.json` gained `parentAgentId`); parsers must treat every field as optional, and unknown types must surface in parse stats so drift is visible.
- Claude Code has no end-of-session marker, so `idle`, `stale` and `finished` for main sessions are time-based guesses; the windows are flags so they can be tuned.
- Oh My Pi nesting deeper than one level, the in-place rewrite of its `title` line, and whether `task` can spawn several children in one call were not observed; the design handles them but they are unverified.
- The slash-command built-in list in 5.6 is maintained by hand and will drift; a wrong entry only moves a count between `skills` and `slashCommands`.
