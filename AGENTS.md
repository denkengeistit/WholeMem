# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

WholeMem is a local-only MCP server that unifies Screenpipe screen/audio captures, mem0 semantic memory, Obsidian daily notes, watchdog-based file versioning, and an SLM oracle into a single agent interface. Everything runs locally with no cloud dependencies.

## Build & Run Commands

```bash
# Install in editable mode (requires Python 3.10+)
uv pip install -e .

# Run the MCP server (stdio transport)
python -m wholemem_mcp.server
# or via entrypoint:
wholemem-mcp
```

There is no test suite, linter, or formatter configured yet.

## Configuration

Config is loaded from `config.yaml` (searched in CWD, then `~/.wholemem/`) and can be overridden with `WHOLEMEM_*` environment variables. See `config.yaml.example` for all options. The config loading logic lives in `src/wholemem_mcp/config.py`.

## MCP Tool Surface

Six tools, designed to minimize agent decision overhead:

- `what_are_we_doing` — Orientation briefing: file state + sessions + open tasks + mem0 auto-search
- `what_happened` — File change history from version store (JSON, no SLM)
- `what_did_we_do` — Narrative history across Screenpipe + mem0 + file changes (SLM-summarized)
- `fix_this` — File recovery via oracle analysis (dry_run=true by default)
- `we_did_this` — Log completion: mem0 write + optional task complete + optional daily note
- `remember_this` — Manual mem0 fact injection

## Architecture

The package lives under `src/wholemem_mcp/` with these sub-packages:

- **server.py** — FastMCP server definition, 6 MCP tool handlers, and the async lifespan that wires all components together. The lifespan creates service instances and injects them into the MCP context via `lifespan_context` dict.
- **config.py** — Pydantic models for all configuration sections. `load_config()` merges YAML file defaults with `WHOLEMEM_*` env var overrides. The root model is `WholeMemConfig`.
- **daemon.py** — Background async loop started as `asyncio.create_task` in the server lifespan. Runs every N minutes: Screenpipe fetch → SLM summarize → mem0 store → Obsidian append → file change sync to mem0.
- **screenpipe.py** — `ScreenpipeProcess` (manages Screenpipe subprocess lifecycle) and `ScreenpipeClient` (async HTTP client for the REST API).
- **summarizer.py** — Calls any OpenAI-compatible chat completions endpoint via `openai.AsyncOpenAI`. Two modes: plain text and Markdown bullets.
- **memory.py** — Wraps `mem0.Memory` with local-only config (on-disk Qdrant, LM Studio provider). `mem0` is imported lazily. Telemetry disabled by default.
- **obsidian.py** — Reads/writes `YYYY-MM-DD.md` daily note files with YAML frontmatter.
- **fs/** — From wawd: `watcher.py` (watchdog-based directory monitoring with debounce + thread-safe pending queue), `version_store.py` (SQLite file version history with snapshots and time-travel), `blob_store.py` (content-addressed zstd-compressed storage).
- **oracle/** — From wawd: `oracle.py` (main interface for briefings, history, restoration), `context.py` (tiered context assembly with diffs), `session_tracker.py` (implicit agent sessions in SQLite), `restorer.py` (version-based file recovery with snapshot safety), `backends/openai_compat.py` (single backend using the shared LLM config).
- **tasks/** — From wawd: `store.py` (TASKS.md parser supporting Obsidian Tasks format: 🆔 IDs, ⛔ dependencies, 📅 dates, [assignee::], [status::]).

### Data Flow

All MCP tool handlers access shared service instances through `ctx.request_context.lifespan_context["<key>"]`. The lifespan creates one instance of each service at startup and tears down in reverse order on shutdown.

### Key Constraints

- **stdout is reserved** for MCP stdio transport — all logging goes to stderr.
- **mem0 is a heavy import** — imported inside `MemoryStore._init_memory()`, not at module level.
- The daemon gracefully degrades: if the summarizer or Screenpipe is unavailable, it falls back to storing raw metadata.
- The watcher uses a `threading.Lock` to protect `_pending` mutations from the watchdog handler thread.
- `fix_this` defaults to `dry_run=true` — agents must explicitly set `dry_run=false` to execute restorations.
- Task IDs use the Obsidian Tasks `🆔` emoji format (or `[id::]` Dataview syntax), not line numbers.
