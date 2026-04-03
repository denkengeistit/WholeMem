# Standalone WholeMem Service + Streamlit UI + HTTP MCP Transport
## Problem
Currently WholeMem runs as an MCP stdio server — each MCP client (Warp, Claude) spawns its own process with its own Qdrant lock, Screenpipe instance, watcher, and daemon. This causes:
* Qdrant embedded-mode file lock conflicts when multiple clients connect
* Duplicate Screenpipe processes that the user can't control
* No way for the user to start/stop screen capture independently
* Zombie processes when clients disconnect without clean shutdown
## Current State
* `server.py` — FastMCP server with `mcp.run()` (stdio-only), lifespan wires all services
* `config.py` — Pydantic config loaded from `config.yaml` + `WHOLEMEM_*` env vars
* `.warp/.mcp.json` — launches `wholemem-mcp` as a subprocess per client
* No Streamlit UI exists
* MCP SDK already supports `streamable-http` transport (FastMCP has `run_streamable_http_async()`, defaults to `127.0.0.1:8000`)
## Architecture Change
Split into three layers:
1. **Core service layer** (`service.py`) — a long-lived async process that owns all shared state: config, Screenpipe, mem0/Qdrant, watcher, version store, oracle, daemon. Exposes an internal API (direct Python calls) to both the MCP layer and the Streamlit UI.
2. **MCP transport layer** (`server.py`) — thin FastMCP wrapper over the service layer, using `streamable-http` transport on `127.0.0.1:8767`. MCP clients connect to `http://localhost:8767/mcp`.
3. **Streamlit UI** (`ui/app.py`) — dashboard that connects to the same service layer (in-process or via a small REST sidecar). Controls start/stop of Screenpipe, shows status, surfaces the 6 tool functions for manual use.
## Proposed Changes
### 1. New `service.py` — Core Service
Extract the service initialization logic from `app_lifespan` into a `WholeMemService` class:
* `async def start()` — initializes all components (what `app_lifespan` does before `yield`)
* `async def stop()` — tears down all components (what `app_lifespan` does in `finally`), with each step wrapped in try/except for isolation
* Properties/methods exposing the shared instances: `config`, `screenpipe`, `memory`, `oracle`, `watcher`, `version_store`, `session_tracker`, `task_store`, `summarizer`, `obsidian`
* `async def status()` → dict of component health
* Methods mirroring the 6 MCP tool operations (called by both MCP handlers and Streamlit)
* Signal handling (SIGTERM/SIGINT) for graceful standalone shutdown
### 2. Update `server.py` — MCP over HTTP
* `app_lifespan` creates a `WholeMemService` instance, calls `start()`, yields its internals, calls `stop()` in finally
* `mcp = FastMCP("wholemem_mcp", lifespan=app_lifespan, host="127.0.0.1", port=8767)`
* `main()` calls `mcp.run(transport="streamable-http")`
* Tool handlers delegate to service methods
* Add a new config section `ServerConfig` with `host`, `port`, `transport` fields
### 3. New `ui/app.py` — Streamlit Dashboard
Pages/sections:
* **Status** — component health (Screenpipe, LLM, mem0, watcher, Obsidian), active sessions, daemon last-run
* **Screenpipe Control** — start/stop toggle, recording indicator
* **Orientation** (`what_are_we_doing`) — workspace selector, briefing display
* **History** (`what_happened` + `what_did_we_do`) — time range slider, file filter, narrative vs. structured toggle
* **Recovery** (`fix_this`) — problem description input, dry-run preview, execute button
* **Memory** (`we_did_this` + `remember_this`) — log completion form, manual fact entry, memory search
* **Config** — read-only view of active configuration
The Streamlit app runs in the same process as the service (or imports it directly). We use `@st.cache_resource` to hold a singleton `WholeMemService`.
### 4. New `__main__.py` — Unified Entrypoint
A single entrypoint that starts both the MCP HTTP server and the Streamlit UI:
* `wholemem-server` — starts the standalone service + MCP HTTP transport
* `wholemem-ui` — starts the Streamlit dashboard (which internally starts or connects to the service)
* Or a combined mode that runs both
For the standalone server, we'll use uvicorn directly to serve the FastMCP Starlette app alongside a small health/control REST endpoint.
### 5. Update `.warp/.mcp.json`
Change from subprocess-based stdio to HTTP client:
```json
{
  "mcpServers": {
    "wholemem": {
      "url": "http://localhost:8767/mcp"
    }
  }
}
```
### 6. Fix shutdown issues (from earlier review)
Implement in `WholeMemService.stop()`:
* Each cleanup step in its own try/except
* `Observer.join(timeout=5)` in watcher
* Flush pending watcher events before stopping
* End active sessions before closing DB
* Overall shutdown timeout (30s)
### 7. Dependencies
Add to `pyproject.toml`:
* `streamlit>=1.35.0`
* `uvicorn>=0.30.0` (needed by FastMCP for HTTP transport)
### 8. Config additions
New `ServerConfig` section in `WholeMemConfig`:
```yaml
server:
  host: "127.0.0.1"
  port: 8767
  transport: "streamable-http"  # or "sse" or "stdio"
```
## File Summary
* `src/wholemem_mcp/service.py` — NEW: core service class
* `src/wholemem_mcp/server.py` — MODIFY: thin MCP wrapper over service
* `src/wholemem_mcp/ui/__init__.py` — NEW
* `src/wholemem_mcp/ui/app.py` — NEW: Streamlit dashboard
* `src/wholemem_mcp/config.py` — MODIFY: add `ServerConfig`
* `src/wholemem_mcp/fs/watcher.py` — MODIFY: fix shutdown (flush + join timeout)
* `pyproject.toml` — MODIFY: add deps + new entrypoints
* `.warp/.mcp.json` — MODIFY: switch to HTTP URL
* `config.yaml.example` — MODIFY: add server section
* `AGENTS.md` / `README.md` — MODIFY: update architecture docs
