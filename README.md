# WholeMem

**Local-only, privacy-respecting memory tool for AI agents** — an open-source alternative to [Pieces OS](https://github.com/pieces-app).

WholeMem is an MCP (Model Context Protocol) server that integrates [Screenpipe](https://github.com/screenpipe/screenpipe) screen/audio captures with [mem0](https://github.com/mem0ai/mem0) semantic memory, adds a lightweight summarization layer using a local SLM, and writes rolling daily notes in [Obsidian](https://obsidian.md) format.

**Everything runs locally. No data leaves your machine.**

## Architecture

```
Screenpipe (localhost:3030)
    │  screen captures, audio transcriptions, UI events
    ▼
Summarizer (OpenAI-compatible SLM — Qwen3-1.7B via LM Studio / vLLM / Ollama)
    │  condenses + timestamps activity
    ▼
mem0 (local Qdrant vector store + fact extraction)
    │  semantic memory with search
    ▼
Obsidian Daily Notes (YYYY-MM-DD.md in your vault)
    │
    ▼
MCP Server (stdio) → any MCP-compatible agent (Claude, Cursor, Warp, etc.)
```

### Background Daemon

A background task runs every 15 minutes (configurable):
1. Fetches recent activity from Screenpipe
2. Summarizes it using your local SLM
3. Stores extracted facts in mem0
4. Appends a timestamped entry to today's Obsidian daily note

## Prerequisites

| Component | Purpose | Install |
|-----------|---------|----------|
| **Screenpipe** | Screen + audio capture | `npx screenpipe@latest record` or [desktop app](https://screenpi.pe) |
| **Ollama** | Local embeddings | `curl -fsSL https://ollama.com/install.sh \| sh` then `ollama pull nomic-embed-text` |
| **LM Studio / vLLM / Ollama** | Summarization LLM | Any OpenAI-compatible server with a small model (e.g. Qwen3-1.7B) |
| **Python 3.10+** | Runtime | System package manager |
| **Obsidian** (optional) | Daily notes viewer | [obsidian.md](https://obsidian.md) |

## Installation

```bash
# Clone the repo
git clone https://github.com/your-user/WholeMem.git
cd WholeMem

# Install with pip (editable mode for development)
pip install -e .

# Or with uv
uv pip install -e .
```

## Configuration

Copy the example config and edit:

```bash
cp config.yaml.example config.yaml
```

### config.yaml

```yaml
screenpipe:
  url: "http://localhost:3030"

llm:
  base_url: "http://localhost:1234/v1"   # LM Studio default
  model: "qwen3-1.7b"
  api_key: "lm-studio"                   # local servers accept any string

embedder:
  provider: "ollama"                      # or "openai" for any compatible endpoint
  model: "nomic-embed-text"
  base_url: "http://localhost:11434"
  # embedding_dims: 768

mem0:
  user_id: "default_user"
  # qdrant_path: "~/.wholemem/qdrant"

obsidian:
  vault_path: "~/Documents/Obsidian"
  daily_notes_subfolder: "Daily Notes"

daemon:
  interval_minutes: 15
```

### Environment Variables

All settings can be overridden with `WHOLEMEM_` prefixed env vars:

```bash
export WHOLEMEM_LLM_BASE_URL="http://localhost:8000/v1"  # vLLM
export WHOLEMEM_LLM_MODEL="Qwen/Qwen3-1.7B"
export WHOLEMEM_OBSIDIAN_VAULT_PATH="~/my-vault"
export WHOLEMEM_DAEMON_INTERVAL=30
```

## Usage

### Run the MCP server

```bash
# Direct execution
python -m wholemem_mcp.server

# Or via the installed entrypoint
wholemem-mcp
```

### Add to Warp

```bash
# In Warp settings → MCP Servers, add:
# Command: wholemem-mcp
# Transport: stdio
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

## MCP Tools

| Tool | Description |
|------|-------------|
| `wholemem_search` | Semantic search across all stored memories |
| `wholemem_search_screenpipe` | Query Screenpipe captures (screen, audio, UI) |
| `wholemem_add_memory` | Manually add a fact or observation to memory |
| `wholemem_get_timeline` | Get chronological activity timeline |
| `wholemem_get_daily_note` | Read an Obsidian daily note |
| `wholemem_summarize_recent` | On-demand summarize recent activity via SLM |
| `wholemem_status` | Health check all components |
| `wholemem_sync_now` | Trigger immediate Screenpipe → mem0 → Obsidian sync |

### Example Queries

Once connected to an MCP client, you can ask:

- *"What was I working on in the last hour?"* → `wholemem_get_timeline`
- *"Search my memory for anything about the API redesign"* → `wholemem_search`
- *"Remember that I decided to use PostgreSQL for the new project"* → `wholemem_add_memory`
- *"Summarize what I did this morning"* → `wholemem_summarize_recent`
- *"Show me today's daily note"* → `wholemem_get_daily_note`
- *"What did I see on screen about pricing?"* → `wholemem_search_screenpipe`

## LLM Options

WholeMem works with any OpenAI-compatible API endpoint. Recommended setups:

### LM Studio (easiest)
1. Download [LM Studio](https://lmstudio.ai)
2. Load Qwen3-1.7B (or any small model)
3. Start the server → default at `http://localhost:1234/v1`

### Ollama
```bash
ollama pull qwen3:1.7b
# Ollama serves OpenAI-compatible API at http://localhost:11434/v1
```

Set in config:
```yaml
llm:
  base_url: "http://localhost:11434/v1"
  model: "qwen3:1.7b"
```

### vLLM
```bash
vllm serve Qwen/Qwen3-1.7B --port 8000
```

Set in config:
```yaml
llm:
  base_url: "http://localhost:8000/v1"
  model: "Qwen/Qwen3-1.7B"
```

## Privacy

- **All data stays local** — Screenpipe captures, mem0 vectors, Obsidian notes, and LLM inference all run on your machine
- **No cloud APIs required** — works entirely offline with Ollama
- **No telemetry** — WholeMem sends no data anywhere
- **You own your data** — everything is stored in standard formats (SQLite, Qdrant, Markdown) that you can inspect, export, or delete at any time

## License

MIT
