# WholeMem

**Local-only memory + workspace awareness for AI agents.**

WholeMem is an MCP server that unifies [Screenpipe](https://github.com/screenpipe/screenpipe) screen/audio captures, [mem0](https://github.com/mem0ai/mem0) semantic memory, [Obsidian](https://obsidian.md) daily notes, watchdog-based file versioning, and an SLM oracle into a single agent interface.

**Everything runs locally. No data leaves your machine.**

## Architecture

```
Screenpipe (localhost:3030)              Workspace directory
    │  screen, audio, UI events              │  file creates, edits, deletes
    ▼                                        ▼
Summarizer (local SLM)              WAWDWatcher (watchdog + SQLite)
    │                                        │
    ▼                                        ▼
mem0 (Qdrant vectors)               VersionStore + BlobStore (zstd)
    │                                        │
    ▼                                        ▼
Obsidian Daily Notes                 Oracle (SLM-powered briefings,
    │                                  history, file restoration)
    ▼                                        │
    └──────────── MCP Server (stdio) ────────┘
                      │
              6 tools → any MCP client
              (Warp, Claude, Cursor, etc.)
```

### Background Daemon

Runs every 15 minutes (configurable):
1. Fetches recent activity from Screenpipe
2. Summarizes via local SLM
3. Stores extracted facts in mem0
4. Appends to today's Obsidian daily note
5. Syncs file changes from the version store into mem0

## MCP Tools

Six tools — designed to minimize agent decision overhead.

| Tool | Purpose |
|------|---------|
| `what_are_we_doing` | Orientation briefing: file state + sessions + open tasks + mem0 search |
| `what_happened` | File change history from version store (JSON, no SLM) |
| `what_did_we_do` | Narrative history across Screenpipe + mem0 + file changes |
| `fix_this` | File recovery via oracle analysis (`dry_run=true` by default) |
| `we_did_this` | Log completion: mem0 write + optional task complete + daily note |
| `remember_this` | Manual memory injection |

### Example Agent Interactions

- *"What are we working on?"* → `what_are_we_doing`
- *"What changed in config.py in the last hour?"* → `what_happened`
- *"Summarize what happened this morning"* → `what_did_we_do`
- *"The config file broke, revert it"* → `fix_this`
- *"We finished the auth migration"* → `we_did_this`
- *"Remember that we chose PostgreSQL"* → `remember_this`

## Prerequisites

| Component | Purpose | Install |
|-----------|---------|---------|
| **Screenpipe** | Screen + audio capture | `npx screenpipe@latest record` or [desktop app](https://screenpi.pe) |
| **LM Studio / vLLM** | Inference + embeddings | Any OpenAI-compatible server with a chat model and an embedding model |
| **Python 3.10+** | Runtime | System package manager or `uv` |
| **Obsidian** (optional) | Daily notes viewer | [obsidian.md](https://obsidian.md) |

## Installation

```bash
git clone https://github.com/denkengeistit/WholeMem.git
cd WholeMem
uv pip install -e .
```

## Configuration

```bash
cp config.yaml.example config.yaml
```

All settings can be overridden with `WHOLEMEM_*` environment variables.

### Key Sections

```yaml
screenpipe:
  url: "http://localhost:3030"
  managed: true                    # start/stop Screenpipe with the server
  disable_telemetry: true

llm:
  base_url: "http://localhost:1234/v1"
  model: "qwen3-1.7b"
  api_key: "lm-studio"

embedder:
  provider: "openai"               # or "ollama"
  model: "nomic-embed-text"
  base_url: "http://localhost:1234/v1"

watcher:
  enabled: true
  path: "~/Projects"               # workspace root to watch
  exclude: [".git/", "node_modules/", "__pycache__/", "*.pyc"]

versioning:
  compression_level: 3
  history_depth: 3                 # max versions kept per file

oracle:
  history_depth: 50
  session_timeout_minutes: 30

obsidian:
  vault_path: "~/Documents/Obsidian"
  daily_notes_subfolder: "Daily Notes"

daemon:
  interval_minutes: 15
```

## Usage

### Run the MCP server

```bash
python -m wholemem_mcp.server
# or:
wholemem-mcp
```

Screenpipe starts automatically if `screenpipe.managed: true`.

### Add to Warp

Add to `.warp/.mcp.json` in your project:

```json
{
  "mcpServers": {
    "wholemem": {
      "command": "/path/to/.venv/bin/wholemem-mcp",
      "args": [],
      "env": {
        "WHOLEMEM_LLM_BASE_URL": "http://your-lm-studio:1234/v1",
        "WHOLEMEM_LLM_MODEL": "your-model",
        "WHOLEMEM_WATCHER_PATH": "/path/to/workspace"
      }
    }
  }
}
```

### Add to Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wholemem": {
      "command": "wholemem-mcp",
      "args": []
    }
  }
}
```

## Task Management

WholeMem reads `TASKS.md` from the workspace root using the [Obsidian Tasks](https://publish.obsidian.md/tasks) format:

```markdown
- [ ] Implement auth middleware 📅 2026-04-15 🆔 abc123
- [ ] Write integration tests ⛔ abc123 [assignee:: agent-1]
- [x] Set up CI pipeline ✅ 2026-04-01
```

Tasks are surfaced in `what_are_we_doing` briefings and can be completed via `we_did_this` using the `🆔` task ID.

## LLM Options

WholeMem works with any OpenAI-compatible endpoint. The same server handles summarization, mem0 fact extraction, embeddings, and oracle queries.

### LM Studio (recommended)
1. Download [LM Studio](https://lmstudio.ai)
2. Load a chat model (e.g. Qwen3-4B) and an embedding model (e.g. nomic-embed-text)
3. Start the server → default at `http://localhost:1234/v1`

### vLLM
```bash
vllm serve Qwen/Qwen3-1.7B --port 8000
```

## Privacy

- **All data stays local** — Screenpipe captures, mem0 vectors, Obsidian notes, file versions, and LLM inference all run on your machine
- **No cloud APIs required** — works entirely offline
- **No telemetry** — mem0 telemetry disabled by default, Screenpipe launched with `--disable-telemetry`
- **You own your data** — SQLite, Qdrant, Markdown — standard formats you can inspect, export, or delete

## License

MIT
