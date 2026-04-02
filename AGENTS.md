# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

WholeMem is a local-only MCP (Model Context Protocol) server that connects Screenpipe screen/audio captures → local SLM summarization → mem0 semantic memory → Obsidian daily notes. It runs entirely on the user's machine with no cloud dependencies.

## Build & Run Commands

```bash
# Install in editable mode
pip install -e .
# or: uv pip install -e .

# Run the MCP server (stdio transport)
python -m wholemem_mcp.server
# or via entrypoint:
wholemem-mcp
```

There is no test suite, linter, or formatter configured yet.

## Configuration

Config is loaded from `config.yaml` (searched in CWD, then `~/.wholemem/`) and can be overridden with `WHOLEMEM_*` environment variables. See `config.yaml.example` for all options. The config loading logic lives in `src/wholemem_mcp/config.py`.

## Architecture

The package lives under `src/wholemem_mcp/` and is structured as a pipeline:

- **server.py** — FastMCP server definition, all 8 MCP tool handlers, and the async lifespan that wires components together. The lifespan creates all service objects and injects them into the MCP context via `lifespan_context` dict. This is the main entry point (`main()` calls `mcp.run()`).
- **config.py** — Pydantic models for all configuration sections. `load_config()` merges YAML file defaults with `WHOLEMEM_*` env var overrides. The root model is `WholeMemConfig`.
- **daemon.py** — Background async loop (`run_daemon`) started as an `asyncio.create_task` in the server lifespan. Runs `_run_sync_cycle` every N minutes: Screenpipe fetch → SLM summarize → mem0 store → Obsidian append. `trigger_sync` exposes the same cycle for the `wholemem_sync_now` tool.
- **screenpipe.py** — Contains `ScreenpipeProcess` (manages the Screenpipe subprocess lifecycle — start on server init, SIGINT on shutdown, polls `/health` for readiness) and `ScreenpipeClient` (async HTTP client for the REST API at localhost:3030, wraps `/search` and `/health`).
- **summarizer.py** — Calls any OpenAI-compatible chat completions endpoint (LM Studio, vLLM, Ollama) via `openai.AsyncOpenAI`. Has two summarization modes: plain text (`summarize_activity`) and Markdown bullets for daily notes (`summarize_for_daily_note`). The `_flatten_items` helper converts Screenpipe content dicts into a timestamped transcript.
- **memory.py** — Wraps `mem0.Memory` with local-only config (on-disk Qdrant vector store, OpenAI-compatible embeddings by default). `mem0` is imported lazily due to heavy startup cost. Both the LLM (fact extraction) and embedder default to the same OpenAI-compatible endpoint (e.g. LM Studio).
- **obsidian.py** — Reads/writes `YYYY-MM-DD.md` daily note files with YAML frontmatter in the configured Obsidian vault directory.

### Data Flow

All MCP tool handlers in `server.py` access shared service instances through `ctx.request_context.lifespan_context["<key>"]`. The lifespan creates one instance of each service at startup and tears down the daemon task on shutdown.

### Key Constraints

- **stdout is reserved** for MCP stdio transport — all logging goes to stderr.
- **mem0 is a heavy import** — it's imported inside `MemoryStore._init_memory()`, not at module level.
- Tool input validation uses Pydantic `BaseModel` subclasses defined in `server.py` (e.g. `SearchMemoryInput`, `AddMemoryInput`).
- The daemon gracefully degrades: if the summarizer is unavailable, it falls back to storing raw metadata counts.
