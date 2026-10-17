# WholeMem ledger

Minimal artifact ledger stripped from WholeMem: just the file watcher and a SQLite change DB.

- Watches `~/tasks/<card>/` (one workdir per Vikunja card) with watchdog/inotify.
- Records create/write/delete per file, with deduplicated zlib-compressed content, the card id from the path, and the agent from the dispatcher's run record.
- Read-only queries: `ledger.py log [card]`, `ledger.py diff <id>`, `ledger.py show <id>`.
- `ledger.py mcp` is a read-only stdio MCP server (ledger_log, ledger_diff, ledger_show) for ZeroClaw, which interprets the ledger (the old Oracle role).

No Screenpipe, mem0, Obsidian, UI, or restore tool. The full original is on `main`.
