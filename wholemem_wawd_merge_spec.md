# WholeMem + wawd — Unified MCP Tool Surface
**Implementation Spec · April 2026 · v1.0**

---

## Context

WholeMem (Screenpipe → mem0 → Obsidian) and wawd (watchdog file versioning → SQLite oracle → MCP) are being merged into a single package. This spec defines the unified MCP tool surface that replaces both projects' existing tools.

The guiding constraint: agents perform measurably worse with large tool surfaces. Every tool added is a decision an agent has to make at inference time. The goal is the minimum surface that covers all agent needs without requiring agents to know anything about the underlying architecture.

---

## Tool Surface

Six tools, replacing fourteen. All names follow the same conversational verb-phrase style.

| Tool | Agent moment | Primary sources |
|---|---|---|
| `what_are_we_doing` | Orientation | File state + sessions + open tasks (TASKS.md) + mem0 auto-search |
| `what_happened` | File history | Version store — precise, scoped, technical |
| `what_did_we_do` | Narrative history | Screenpipe timeline + mem0 recent facts + daily note summary |
| `fix_this` | Recovery | Version store restore; task completion as side effect |
| `we_did_this` | Log completion | mem0 write + TASKS.md complete + optional Obsidian append |
| `remember_this` | Manual injection | mem0 write, any source/schema |

---

## Tool Specifications

---

### `what_are_we_doing`
*Orientation briefing — call this at the start of any session*

**Purpose**

Returns a structured briefing that gives an agent everything it needs to orient in a workspace. Replaces `what_are_we_doing` (wawd) and absorbs `wholemem_search`, `wholemem_search_screenpipe`, and `get_tasks`. Memory search and task injection happen inside the oracle's ContextBuilder — the agent does not need to issue separate queries.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `workspace` | string | yes | Absolute path to the workspace directory to brief on. |
| `query` | string | no | Optional topic hint. If provided, biases the mem0 semantic search toward this topic. Default: derives from recent file activity. |
| `depth` | string | no | `"brief"` (default) or `"full"`. Brief returns a 200–400 word summary. Full includes per-file version counts and the full task list. |

**Output**

Plain-text briefing structured as:
- Workspace state — modified files since last session, active agents, session duration
- Open tasks — items from TASKS.md not yet marked complete, filtered to this workspace
- Relevant memory — top 3–5 mem0 results for the derived or supplied query
- Suggested next steps — oracle-generated, 2–3 bullets max

**Implementation notes**
- mem0 search runs automatically on every invocation — no separate `wholemem_search` call needed
- TASKS.md is read from the workspace root; missing file is silently ignored
- `claim_task` is eliminated — task claiming is not enforced; agents simply start working
- Session tracking uses the existing wawd `SessionTracker`; no changes needed
- If mem0 is unavailable, the briefing omits the memory section and logs a warning — it does not fail

---

### `what_happened`
*File change history — precise, scoped, technical*

**Purpose**

Queries the version store for file-level change history. This is the technical, precise complement to `what_did_we_do`. An agent uses `what_happened` when it needs to know exactly what changed in a specific file or directory — not a narrative summary, but a changelog. Retained from wawd unchanged in behaviour.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `workspace` | string | yes | Absolute path to the workspace. |
| `path` | string | no | Relative path to a file or directory. Omit to query the entire workspace. |
| `minutes` | int | no | Look back N minutes (default: 60, max: 1440). |
| `agent` | string | no | Filter to changes made by a specific agent ID. |
| `limit` | int | no | Max versions to return (default: 20, max: 100). |

**Output**

JSON array of version records, each containing: `path`, `agent_id`, `timestamp`, `operation` (create/modify/delete), `size_bytes`, `version_id`.

**Implementation notes**
- Calls `VersionStore.get_history()` directly — no SLM involved, no summarization
- This is the only tool with a JSON output; all others return plain text. Intentional: agents querying history need structured data to reason about specific versions for `fix_this`.

---

### `what_did_we_do`
*Narrative history — unified across screen, memory, and files*

**Purpose**

Returns a human-readable narrative of recent activity synthesized across all available sources: Screenpipe screen/audio captures, mem0 stored facts, and file change summaries from the version store. Replaces `wholemem_get_timeline`, `wholemem_summarize_recent`, and `wholemem_get_daily_note`. The oracle's Summarizer merges all sources into a single chronological narrative.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `minutes` | int | no | How far back to look (default: 60, max: 1440). |
| `workspace` | string | no | If provided, file change narrative is scoped to this workspace. Otherwise file changes are omitted. |
| `focus` | string | no | Optional topic to filter the narrative. E.g. `"auth"` narrows to activity mentioning auth-related content. |

**Output**

Plain-text narrative structured as a timestamped chronology. The Summarizer is instructed to interleave screen activity and file changes in time order, not in separate sections.

**Implementation notes**
- If Screenpipe is unavailable, returns file changes and mem0 facts only — no error
- If no workspace is provided or the watcher is not running, returns screen + memory only
- Daily note content is read by the oracle internally for context — not returned verbatim
- Uses the same OpenAI-compat endpoint as the rest of the oracle — no additional backend needed

---

### `fix_this`
*Recovery — file restore with natural language problem description*

**Purpose**

Accepts a natural language description of a problem and lets the oracle determine the appropriate recovery action. The oracle uses ContextBuilder to identify the relevant file(s), selects the appropriate historical version via the version store, and executes the restore. Task completion is a side effect: if the problem description maps to an open task in TASKS.md, that task is marked complete. Retained from wawd; behaviour unchanged.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `workspace` | string | yes | Absolute path to the workspace. |
| `description` | string | yes | Natural language description of the problem. E.g. `"the config file was broken 20 minutes ago, revert it"`. |
| `dry_run` | bool | no | If true (default), return the restoration plan without executing it. Agent must call again with `dry_run=false` to apply. |

**Output**

Plain text. In dry_run mode: the proposed restoration plan (files, versions, rationale). In execute mode: confirmation of what was restored and any task completions triggered.

**Implementation notes**
- `dry_run` defaults to `true` — agents must explicitly set `dry_run=false` to execute
- The watcher is paused during restore to suppress self-versioning, then resumed
- Task completion side effect: oracle checks if description semantically matches any open TASKS.md item; if so, marks it complete. This replaces the `complete_task` MCP tool.
- The Perplexity review flagged `fix_this` as the highest-risk tool. The `dry_run` default is the primary mitigation — the two-call pattern (plan then execute) is sufficient.

---

### `we_did_this`
*Log completion — write to memory, tasks, and daily note in one call*

**Purpose**

A dual-write tool for logging completed work. Stores a fact in mem0, optionally marks a TASKS.md item complete, and optionally appends to today's Obsidian daily note. Replaces `complete_task` (wawd) and the workflow-completion use case of `wholemem_add_memory`. The distinction from `remember_this`: `we_did_this` is about completed work with optional task linkage; `remember_this` is for arbitrary facts with no workflow semantics.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `summary` | string | yes | Description of what was accomplished. Stored as a mem0 fact. |
| `workspace` | string | no | If provided, links the fact to this workspace in mem0 metadata. |
| `task_id` | string | no | TASKS.md task ID to mark complete. If omitted, no task state is changed. |
| `append_note` | bool | no | If true, appends the summary to today's Obsidian daily note as a bullet. Default: false. |

**Output**

Plain text confirmation of what was written and where.

**Implementation notes**
- mem0 write always happens — it is not conditional on `task_id` or `append_note`
- `task_id` lookup is against TASKS.md in the provided workspace root; if no workspace is provided and `task_id` is set, returns an error
- `append_note` writes via `ObsidianWriter` using the same format as existing daemon entries

---

### `remember_this`
*Manual memory injection — escape hatch for arbitrary facts*

**Purpose**

Stores an arbitrary fact or observation in mem0. No task semantics, no file linkage, no daily note write. This is the integration flexibility escape hatch — for cases where an agent or the user wants to inject a fact that does not correspond to completed work. Replaces `wholemem_add_memory`.

**Input**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | The fact or observation to store. |
| `category` | string | no | Optional tag for filtering. E.g. `"decision"`, `"architecture"`, `"bug"`. |
| `source` | string | no | Optional provenance label. Default: `"manual"`. |

**Output**

Plain text confirmation including the mem0-assigned ID for the stored fact.

**Implementation notes**
- Thin wrapper over `MemoryStore.add()` — no SLM, no summarization, no side effects
- `category` and `source` are stored as mem0 metadata for filtering in future searches

---

## Retired Tools

| Tool | Disposition |
|---|---|
| `wholemem_search` | Absorbed into `what_are_we_doing`. mem0 search runs automatically on orientation. |
| `wholemem_search_screenpipe` | Absorbed into `what_did_we_do`. Screenpipe is one source in the unified history call. |
| `wholemem_add_memory` | Split: work-completion writes go to `we_did_this`; arbitrary facts go to `remember_this`. |
| `wholemem_get_timeline` | Absorbed into `what_did_we_do`. |
| `wholemem_get_daily_note` | Oracle reads the daily note internally. Agents and users can read the file directly. |
| `wholemem_summarize_recent` | Absorbed into `what_did_we_do`. |
| `wholemem_status` | CLI-only. Not relevant to agent sessions. |
| `wholemem_sync_now` | CLI-only. Daemon runs on its own schedule. |
| `get_tasks` | Absorbed into `what_are_we_doing`. Open tasks included in every orientation briefing. |
| `claim_task` | Eliminated. No task locking needed in a single-developer + agents context. |
| `complete_task` | Absorbed as side effect of `fix_this` (implicit) and `we_did_this` (explicit via `task_id`). |

---

## Architecture Notes

### Package structure

wawd modules copy into `src/wholemem_mcp/` as sub-packages. No renaming of internal modules — only the MCP tool names change.

| Path | Contents |
|---|---|
| `src/wholemem_mcp/fs/` | `watcher.py`, `version_store.py`, `blob_store.py` — from wawd unchanged |
| `src/wholemem_mcp/oracle/` | `oracle.py`, `context.py`, `restorer.py`, `session_tracker.py`, `prompts.py` — from wawd; Ollama and llama.cpp backends removed, single `openai_compat.py` backend kept |
| `src/wholemem_mcp/tasks/` | `store.py` — TASKS.md parser from wawd; `TaskStore` used internally by oracle and `we_did_this` |
| `src/wholemem_mcp/server.py` | Replaces both projects' server.py. Six tool handlers. Lifespan wires all components. |
| `src/wholemem_mcp/daemon.py` | Unchanged. Background sync cycle (Screenpipe → mem0 → Obsidian) continues. |

### Lifespan wiring

The merged `app_lifespan` adds wawd components to the existing WholeMem lifespan context dict. New keys added:

- `"db"` — aiosqlite connection (per-workspace, derived from workspace path + SHA1 hash, matching wawd's existing naming scheme)
- `"blob_store"` — `BlobStore` instance
- `"version_store"` — `VersionStore` instance
- `"session_tracker"` — `SessionTracker` instance
- `"watcher"` — `WAWDWatcher` instance (started if `watcher.enabled` in config)
- `"oracle"` — `Oracle` instance with `ContextBuilder` + `Restorer` wired to `openai_compat` backend
- `"task_store"` — `TaskStore` instance (reads TASKS.md lazily per workspace)

### Config additions

Two new sections added to `WholeMemConfig`. All existing sections unchanged.

```yaml
watcher:
  enabled: true
  path: ~/Scratch            # workspace root to watch
  exclude:
    - "**/.git/**"
    - "**/__pycache__/**"
    - "**/*.pyc"

versioning:
  compression_level: 3       # zstd compression level
  db_path: ~/.wholemem/      # per-workspace DBs written here
  history_depth: 3           # max versions kept per file
```

### Backend unification

wawd currently supports three oracle backends: Ollama, llama.cpp, and `openai_compat`. The merge removes Ollama and llama.cpp. The single `openai_compat.py` backend is configured with the existing `LLMConfig` — the same LM Studio endpoint already used by WholeMem's `Summarizer`. No new config fields needed.

### Daemon extension

The existing daemon sync cycle is extended with one additional step: after the mem0 write, query `VersionStore.get_changes_since()` for file changes in the same window, summarize them via the existing `Summarizer`, and store the result in mem0 with `source: "file_watcher"` metadata. This makes file activity visible to `what_did_we_do`'s mem0 query without any changes to the tool itself.

### Hardening items (do in same pass)

These are pre-existing wawd issues — do not defer past the merge:

- **WAL mode** — add `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` to the aiosqlite connection setup
- **`_pending` thread safety** — add a `threading.Lock` around `WAWDWatcher._pending` mutations; the GIL is not a substitute
- **README drift** — the wawd README still mentions FUSE-T; update to reflect watchdog before merging

---

## Dependency Changes

Add to `pyproject.toml`:

```
watchdog>=4.0.0
aiosqlite>=0.20.0
zstandard>=0.22.0
```

No packages removed from WholeMem's existing dependencies. wawd's Ollama-specific dependencies are dropped; the `openai` package already present in WholeMem covers the unified backend.

---

*Spec prepared for Warp (Oz agent). Reviewed by Andy + Claude.*
