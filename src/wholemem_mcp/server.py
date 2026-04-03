"""WholeMem MCP Server — unified memory + workspace awareness.

Merges Screenpipe captures, mem0 semantic memory, Obsidian daily notes,
watchdog file versioning, and an SLM oracle into a single MCP interface.

Six tools: what_are_we_doing, what_happened, what_did_we_do,
fix_this, we_did_this, remember_this.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite
from pydantic import BaseModel, Field

from mcp.server.fastmcp import FastMCP

from wholemem_mcp.config import WholeMemConfig, load_config
from wholemem_mcp.daemon import run_daemon, trigger_sync
from wholemem_mcp.fs.blob_store import BlobStore
from wholemem_mcp.fs.version_store import VersionStore
from wholemem_mcp.fs.watcher import WAWDWatcher
from wholemem_mcp.memory import MemoryStore
from wholemem_mcp.obsidian import ObsidianWriter
from wholemem_mcp.oracle.backends.openai_compat import OpenAICompatBackend
from wholemem_mcp.oracle.context import ContextBuilder
from wholemem_mcp.oracle.oracle import Oracle
from wholemem_mcp.oracle.restorer import Restorer
from wholemem_mcp.oracle.session_tracker import SessionTracker
from wholemem_mcp.screenpipe import ScreenpipeClient, ScreenpipeProcess
from wholemem_mcp.summarizer import Summarizer
from wholemem_mcp.tasks import TaskStore

# ---------------------------------------------------------------------------
# Logging — stderr only (stdio transport reserves stdout)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("wholemem")


# ---------------------------------------------------------------------------
# Internal utilities (not MCP-exposed)
# ---------------------------------------------------------------------------

async def check_status(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Health check all components. Used internally by the oracle."""
    config: WholeMemConfig = ctx["config"]
    screenpipe: ScreenpipeClient = ctx["screenpipe"]
    summarizer: Summarizer = ctx["summarizer"]
    memory: MemoryStore = ctx["memory"]
    obsidian: ObsidianWriter = ctx["obsidian"]

    return {
        "screenpipe": {"available": await screenpipe.is_available()},
        "llm": {"available": await summarizer.is_available()},
        "mem0": {"available": memory.is_available()},
        "obsidian": {"available": obsidian.is_available()},
        "watcher": {"enabled": config.watcher.enabled},
    }


def _db_path_for_workspace(config: WholeMemConfig) -> Path:
    """Derive a unique DB path per workspace (matches wawd convention)."""
    ws_path = str(Path(config.watcher.path).expanduser().resolve())
    slug = Path(ws_path).name
    ws_hash = hashlib.sha1(ws_path.encode()).hexdigest()[:8]
    db_dir = Path(config.versioning.db_path).expanduser()
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"wholemem_{slug}_{ws_hash}.db"


# ---------------------------------------------------------------------------
# Lifespan — initialise all components
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Initialise all WholeMem + wawd components."""
    cfg = load_config()

    # --- Screenpipe ---
    sp_process: ScreenpipeProcess | None = None
    if cfg.screenpipe.managed:
        sp_process = ScreenpipeProcess(cfg.screenpipe)
        try:
            await sp_process.start()
        except Exception as exc:
            logger.warning("Failed to start managed Screenpipe: %s", exc)
            sp_process = None

    screenpipe = ScreenpipeClient(cfg.screenpipe)
    summarizer = Summarizer(cfg.llm)
    memory = MemoryStore(cfg)
    obsidian = ObsidianWriter(cfg.obsidian)

    # --- Version store (SQLite + watchdog) ---
    db: aiosqlite.Connection | None = None
    blob_store: BlobStore | None = None
    version_store: VersionStore | None = None
    session_tracker: SessionTracker | None = None
    watcher: WAWDWatcher | None = None
    oracle_obj: Oracle | None = None
    backend: OpenAICompatBackend | None = None

    if cfg.watcher.enabled:
        db_path = _db_path_for_workspace(cfg)
        db = await aiosqlite.connect(str(db_path))

        blob_store = BlobStore(db, cfg.versioning.compression_level)
        await blob_store.init_db()
        version_store = VersionStore(db, blob_store)
        await version_store.init_db()
        session_tracker = SessionTracker(db, cfg.oracle.session_timeout_minutes)
        await session_tracker.init_db()

        # Oracle backend — reuses the LLM config
        backend = OpenAICompatBackend(
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
            api_key=cfg.llm.api_key,
            timeout=300.0,
        )

        context_builder = ContextBuilder(
            version_store, session_tracker, cfg.oracle.history_depth,
        )
        restorer = Restorer(
            version_store, context_builder, backend,
            str(Path(cfg.watcher.path).expanduser()),
        )
        oracle_obj = Oracle(
            version_store, session_tracker, context_builder, restorer,
            backend, str(Path(cfg.watcher.path).expanduser()),
        )

        # Start watcher
        ws_path = str(Path(cfg.watcher.path).expanduser())
        watcher = WAWDWatcher(
            ws_path, version_store, cfg.watcher.exclude,
            session_tracker=session_tracker,
        )
        await watcher.start()
        oracle_obj.set_watcher(watcher)
        restorer.set_watcher(watcher)

        logger.info("Watcher + oracle started for %s", ws_path)

    # --- Task store ---
    task_store: TaskStore | None = None
    if cfg.watcher.enabled:
        task_store = TaskStore(Path(cfg.watcher.path).expanduser())

    # --- Background daemon ---
    daemon_task = asyncio.create_task(
        run_daemon(cfg, screenpipe, summarizer, memory, obsidian,
                   version_store=version_store)
    )

    logger.info("WholeMem MCP server initialised.")

    try:
        yield {
            "config": cfg,
            "screenpipe": screenpipe,
            "summarizer": summarizer,
            "memory": memory,
            "obsidian": obsidian,
            "daemon_task": daemon_task,
            "db": db,
            "blob_store": blob_store,
            "version_store": version_store,
            "session_tracker": session_tracker,
            "watcher": watcher,
            "oracle": oracle_obj,
            "backend": backend,
            "task_store": task_store,
        }
    finally:
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass
        logger.info("Daemon stopped.")

        if watcher:
            await watcher.stop()
        if backend:
            await backend.close()

        memory.close()

        if db:
            await db.close()

        if sp_process is not None:
            await sp_process.stop()

        logger.info("WholeMem shutdown complete.")


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("wholemem_mcp", lifespan=app_lifespan)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class BriefingInput(BaseModel):
    """Input for what_are_we_doing."""
    workspace: str = Field(..., description="Absolute path to the workspace directory.")
    query: Optional[str] = Field(default=None, description="Optional topic hint for mem0 search.")
    depth: str = Field(default="brief", description="'brief' (200-400 words) or 'full'.")


class FileHistoryInput(BaseModel):
    """Input for what_happened."""
    workspace: str = Field(..., description="Absolute path to the workspace.")
    path: Optional[str] = Field(default=None, description="Relative file/directory path.")
    minutes: int = Field(default=60, description="Look back N minutes.", ge=1, le=1440)
    agent: Optional[str] = Field(default=None, description="Filter to a specific agent ID.")
    limit: int = Field(default=20, description="Max versions to return.", ge=1, le=100)


class NarrativeInput(BaseModel):
    """Input for what_did_we_do."""
    minutes: int = Field(default=60, description="How far back to look.", ge=1, le=1440)
    workspace: Optional[str] = Field(default=None, description="Scope file changes to this workspace.")
    focus: Optional[str] = Field(default=None, description="Topic filter for the narrative.")


class FixInput(BaseModel):
    """Input for fix_this."""
    workspace: str = Field(..., description="Absolute path to the workspace.")
    description: str = Field(..., description="Natural language problem description.")
    dry_run: bool = Field(default=True, description="If true, show plan without executing.")


class LogCompletionInput(BaseModel):
    """Input for we_did_this."""
    summary: str = Field(..., description="What was accomplished.", min_length=1)
    workspace: Optional[str] = Field(default=None, description="Workspace to link the fact to.")
    task_id: Optional[str] = Field(default=None, description="TASKS.md 🆔 ID to mark complete.")
    append_note: bool = Field(default=False, description="Append to today's daily note.")


class RememberInput(BaseModel):
    """Input for remember_this."""
    content: str = Field(..., description="Fact or observation to store.", min_length=1)
    category: Optional[str] = Field(default=None, description="Optional tag.")
    source: Optional[str] = Field(default="manual", description="Provenance label.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="what_are_we_doing",
    annotations={
        "title": "Orientation Briefing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def what_are_we_doing(params: BriefingInput) -> str:
    """Get an orientation briefing for a workspace.

    Returns workspace state, open tasks, relevant memories, and suggested
    next steps. Call this at the start of any session.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    oracle_obj: Oracle | None = ctx["oracle"]
    memory: MemoryStore = ctx["memory"]
    task_store: TaskStore | None = ctx["task_store"]

    parts: list[str] = []

    # Oracle briefing (file state + sessions)
    if oracle_obj:
        try:
            result = await oracle_obj.briefing(
                agent_name="agent",
                task=params.query,
                focus=params.query,
            )
            parts.append(result["briefing"])
        except Exception as exc:
            logger.warning("Oracle briefing failed: %s", exc)
            parts.append("[Oracle unavailable]")

    # Open tasks
    if task_store:
        try:
            tasks = task_store.get_tasks()
            if tasks:
                task_lines = ["## Open Tasks"]
                for t in tasks:
                    line = f"- [ ] {t.text}"
                    if t.task_id:
                        line += f" (🆔 {t.task_id})"
                    task_lines.append(line)
                parts.append("\n".join(task_lines))
        except Exception:
            pass

    # mem0 search
    search_query = params.query or "recent activity"
    try:
        memories = memory.search(query=search_query, limit=5)
        if memories:
            mem_lines = ["## Relevant Memory"]
            for m in memories:
                mem_lines.append(f"- {m.get('memory', '')}")
            parts.append("\n".join(mem_lines))
    except Exception as exc:
        logger.warning("mem0 search failed during briefing: %s", exc)

    return "\n\n".join(parts) if parts else "No information available for this workspace."


@mcp.tool(
    name="what_happened",
    annotations={
        "title": "File Change History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def what_happened(params: FileHistoryInput) -> str:
    """Query file-level change history from the version store.

    Returns a JSON array of version records. No SLM involved.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    version_store: VersionStore | None = ctx["version_store"]

    if version_store is None:
        return json.dumps({"error": "Watcher not enabled"})

    since = time.time() - (params.minutes * 60)

    if params.path:
        entries = await version_store.get_history(
            params.path, limit=params.limit, since_timestamp=since,
        )
    elif params.agent:
        entries = await version_store.get_changes_by_agent(params.agent, since=since)
        entries = entries[:params.limit]
    else:
        entries = await version_store.get_changes_since(since)
        entries = entries[:params.limit]

    results = []
    for e in entries:
        results.append({
            "path": e.path,
            "agent_id": e.agent_id,
            "timestamp": datetime.fromtimestamp(e.timestamp, tz=timezone.utc).isoformat(),
            "operation": e.operation,
            "version_id": e.id,
        })

    return json.dumps(results, indent=2)


@mcp.tool(
    name="what_did_we_do",
    annotations={
        "title": "Narrative History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def what_did_we_do(params: NarrativeInput) -> str:
    """Narrative history synthesized across screen captures, memory, and file changes.

    Interleaves Screenpipe activity and file changes chronologically.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    screenpipe: ScreenpipeClient = ctx["screenpipe"]
    summarizer: Summarizer = ctx["summarizer"]
    memory: MemoryStore = ctx["memory"]
    version_store: VersionStore | None = ctx["version_store"]

    sources: list[str] = []

    # Screenpipe timeline
    try:
        items = await screenpipe.get_recent_activity(minutes=params.minutes)
        if items:
            from wholemem_mcp.summarizer import _flatten_items
            transcript = _flatten_items(items)
            sources.append(f"## Screen & Audio Activity\n{transcript}")
    except Exception as exc:
        logger.warning("Screenpipe fetch failed: %s", exc)

    # File changes
    if version_store and params.workspace:
        since = time.time() - (params.minutes * 60)
        try:
            changes = await version_store.get_changes_since(since)
            if changes:
                file_lines = ["## File Changes"]
                for c in changes:
                    ts = datetime.fromtimestamp(c.timestamp, tz=timezone.utc).strftime("%H:%M")
                    file_lines.append(
                        f"[{ts}] {c.operation} {c.path} by {c.agent_id or 'unknown'}"
                    )
                sources.append("\n".join(file_lines))
        except Exception as exc:
            logger.warning("Version store query failed: %s", exc)

    # mem0 recent facts
    try:
        query = params.focus or "recent activity"
        memories = memory.search(query=query, limit=5)
        if memories:
            mem_lines = ["## Stored Facts"]
            for m in memories:
                mem_lines.append(f"- {m.get('memory', '')}")
            sources.append("\n".join(mem_lines))
    except Exception:
        pass

    if not sources:
        return "No activity found in the requested time window."

    # Summarize via SLM
    combined = "\n\n".join(sources)
    try:
        summary = await summarizer.summarize_activity(
            [{"type": "text", "content": {"text": combined, "timestamp": ""}}]
        )
        return summary
    except Exception:
        return combined


@mcp.tool(
    name="fix_this",
    annotations={
        "title": "File Recovery",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def fix_this(params: FixInput) -> str:
    """Restore files to a working state based on a problem description.

    Defaults to dry_run=true — call again with dry_run=false to execute.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    oracle_obj: Oracle | None = ctx["oracle"]
    task_store: TaskStore | None = ctx["task_store"]

    if oracle_obj is None:
        return "Error: Watcher/oracle not enabled. Cannot restore files."

    try:
        result = await oracle_obj.fix(
            problem=params.description,
            dry_run=params.dry_run,
        )
    except Exception as exc:
        return f"Restoration failed: {exc}"

    parts = [f"Action: {result['action_taken']}"]
    if result["files_restored"]:
        parts.append(f"Files: {len(result['files_restored'])}")
        for f in result["files_restored"]:
            parts.append(f"  - {f['path']} → v{f.get('to_version', '?')}")
    parts.append(f"\n{result['explanation']}")

    # Side effect: if description matches an open task, complete it
    if not params.dry_run and task_store:
        try:
            tasks = task_store.get_tasks()
            for t in tasks:
                if params.description.lower() in t.text.lower():
                    task_store.complete_task(line_num=t.line_num)
                    parts.append(f"\n✅ Task completed: {t.text[:80]}")
                    break
        except Exception:
            pass

    return "\n".join(parts)


@mcp.tool(
    name="we_did_this",
    annotations={
        "title": "Log Completion",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def we_did_this(params: LogCompletionInput) -> str:
    """Log completed work to memory, optionally mark a task complete
    and/or append to today's daily note.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    memory: MemoryStore = ctx["memory"]
    obsidian: ObsidianWriter = ctx["obsidian"]
    task_store: TaskStore | None = ctx["task_store"]

    confirmations: list[str] = []

    # Always write to mem0
    metadata: Dict[str, Any] = {"source": "we_did_this"}
    if params.workspace:
        metadata["workspace"] = params.workspace

    try:
        result = memory.add(content=params.summary, metadata=metadata)
        ids = [r.get("id", "?") for r in result.get("results", [])]
        confirmations.append(f"Memory stored (IDs: {', '.join(ids)})")
    except Exception as exc:
        confirmations.append(f"Memory write failed: {exc}")

    # Task completion
    if params.task_id:
        ts = task_store
        if ts is None and params.workspace:
            ts = TaskStore(Path(params.workspace).expanduser())
        if ts:
            try:
                ts.complete_task(task_id=params.task_id)
                confirmations.append(f"Task {params.task_id} marked complete")
            except Exception as exc:
                confirmations.append(f"Task completion failed: {exc}")
        else:
            confirmations.append("Cannot complete task: no workspace provided")

    # Daily note append
    if params.append_note:
        try:
            path = obsidian.append_entry(f"- {params.summary}")
            confirmations.append(f"Appended to daily note: {path}")
        except Exception as exc:
            confirmations.append(f"Daily note write failed: {exc}")

    return "\n".join(confirmations)


@mcp.tool(
    name="remember_this",
    annotations={
        "title": "Remember Fact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remember_this(params: RememberInput) -> str:
    """Store an arbitrary fact or observation in mem0.

    No task semantics, no file linkage, no daily note write.
    """
    ctx = mcp.get_context().request_context.lifespan_context
    memory: MemoryStore = ctx["memory"]

    metadata: Dict[str, Any] = {"source": params.source or "manual"}
    if params.category:
        metadata["category"] = params.category

    try:
        result = memory.add(content=params.content, metadata=metadata)
        ids = [r.get("id", "?") for r in result.get("results", [])]
        return f"Stored (IDs: {', '.join(ids)})"
    except Exception as exc:
        return f"Memory write failed: {exc}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the WholeMem MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
