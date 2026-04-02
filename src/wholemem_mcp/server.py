"""WholeMem MCP Server — local-only privacy-respecting memory tool.

Integrates Screenpipe captures, mem0 storage, SLM summarization,
and Obsidian daily notes into a single MCP interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from mcp.server.fastmcp import FastMCP

from wholemem_mcp.config import WholeMemConfig, load_config
from wholemem_mcp.daemon import run_daemon, trigger_sync
from wholemem_mcp.memory import MemoryStore
from wholemem_mcp.obsidian import ObsidianWriter
from wholemem_mcp.screenpipe import ScreenpipeClient, ScreenpipeProcess
from wholemem_mcp.summarizer import Summarizer

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
# Lifespan — initialise components, start daemon
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Initialise all WholeMem components and start the background daemon."""
    cfg = load_config()

    # Start managed Screenpipe process if configured
    sp_process: ScreenpipeProcess | None = None
    if cfg.screenpipe.managed:
        sp_process = ScreenpipeProcess(cfg.screenpipe)
        await sp_process.start()

    screenpipe = ScreenpipeClient(cfg.screenpipe)
    summarizer = Summarizer(cfg.llm)
    memory = MemoryStore(cfg)
    obsidian = ObsidianWriter(cfg.obsidian)

    # Start background daemon
    daemon_task = asyncio.create_task(
        run_daemon(cfg, screenpipe, summarizer, memory, obsidian)
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
        }
    finally:
        daemon_task.cancel()
        try:
            await daemon_task
        except asyncio.CancelledError:
            pass
        logger.info("WholeMem daemon stopped.")

        memory.close()
        logger.info("mem0 store closed.")

        if sp_process is not None:
            await sp_process.stop()


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("wholemem_mcp", lifespan=app_lifespan)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class SearchMemoryInput(BaseModel):
    """Input for semantic memory search."""
    query: str = Field(..., description="Natural-language search query", min_length=1)
    limit: int = Field(default=10, description="Max results to return", ge=1, le=50)


class SearchScreenpipeInput(BaseModel):
    """Input for querying Screenpipe captures."""
    query: Optional[str] = Field(default=None, description="Search text (optional)")
    content_type: str = Field(
        default="all",
        description="Content type: 'all', 'ocr', 'audio', 'ui', 'audio+ocr'",
    )
    app_name: Optional[str] = Field(default=None, description="Filter by app name")
    minutes_ago: Optional[int] = Field(
        default=None,
        description="Only return items from the last N minutes",
        ge=1,
    )
    limit: int = Field(default=20, description="Max results", ge=1, le=100)


class AddMemoryInput(BaseModel):
    """Input for adding a memory manually."""
    content: str = Field(..., description="Text to remember", min_length=1)
    category: Optional[str] = Field(default=None, description="Optional category tag")


class TimelineInput(BaseModel):
    """Input for retrieving activity timeline."""
    minutes: int = Field(default=60, description="How many minutes back to look", ge=1, le=1440)


class DailyNoteInput(BaseModel):
    """Input for reading a daily note."""
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format (default: today)",
    )


class SummarizeInput(BaseModel):
    """Input for on-demand summarization."""
    minutes: int = Field(default=30, description="Summarize last N minutes of activity", ge=1, le=480)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="wholemem_search",
    annotations={
        "title": "Search Memory",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wholemem_search(params: SearchMemoryInput) -> str:
    """Search across all stored memories using semantic similarity.

    Queries the mem0 vector store for facts, observations, and summaries
    that match the given natural-language query. Returns ranked results
    with relevance scores.

    Args:
        params: SearchMemoryInput with query string and result limit.

    Returns:
        JSON list of matching memories with id, text, score, and metadata.
    """
    ctx = mcp.get_context()
    memory: MemoryStore = ctx.request_context.lifespan_context["memory"]

    results = memory.search(query=params.query, limit=params.limit)
    if not results:
        return f"No memories found matching '{params.query}'."
    return json.dumps(results, indent=2, default=str)


@mcp.tool(
    name="wholemem_search_screenpipe",
    annotations={
        "title": "Search Screenpipe Captures",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wholemem_search_screenpipe(params: SearchScreenpipeInput) -> str:
    """Query Screenpipe for screen captures and audio transcriptions.

    Searches the local Screenpipe database for OCR text, audio transcriptions,
    and UI events. Supports filtering by content type, app, and time range.

    Args:
        params: SearchScreenpipeInput with query, filters, and limit.

    Returns:
        JSON search results from Screenpipe with pagination info.
    """
    ctx = mcp.get_context()
    screenpipe: ScreenpipeClient = ctx.request_context.lifespan_context["screenpipe"]

    from datetime import datetime, timedelta, timezone

    start_time = None
    if params.minutes_ago:
        start_time = (
            datetime.now(timezone.utc) - timedelta(minutes=params.minutes_ago)
        ).isoformat()

    try:
        result = await screenpipe.search(
            query=params.query,
            content_type=params.content_type,
            start_time=start_time,
            app_name=params.app_name,
            limit=params.limit,
        )
        data = result.get("data", [])
        pagination = result.get("pagination", {})

        # Flatten for readability
        items = []
        for item in data:
            ctype = item.get("type", "Unknown")
            content = item.get("content", {})
            entry: Dict[str, Any] = {"type": ctype}

            if ctype == "OCR":
                entry["timestamp"] = content.get("timestamp")
                entry["app"] = content.get("app_name")
                entry["text"] = content.get("text", "")[:500]
            elif ctype == "Audio":
                entry["timestamp"] = content.get("timestamp")
                entry["transcription"] = content.get("transcription", "")[:500]
                entry["device"] = content.get("device_name")
            elif ctype == "UI":
                entry["timestamp"] = content.get("timestamp")
                entry["app"] = content.get("app_name")
                entry["text"] = content.get("text", "")[:300]
            else:
                entry["raw"] = str(content)[:300]

            items.append(entry)

        return json.dumps(
            {"items": items, "total": pagination.get("total", len(items))},
            indent=2,
            default=str,
        )
    except Exception as exc:
        return f"Error querying Screenpipe: {exc}"


@mcp.tool(
    name="wholemem_add_memory",
    annotations={
        "title": "Add Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def wholemem_add_memory(params: AddMemoryInput) -> str:
    """Manually add a fact or observation to long-term memory.

    The text is processed by mem0's LLM to extract and store discrete facts.
    Useful for recording decisions, preferences, or important context.

    Args:
        params: AddMemoryInput with content text and optional category.

    Returns:
        JSON result with extracted memory IDs and events.
    """
    ctx = mcp.get_context()
    memory: MemoryStore = ctx.request_context.lifespan_context["memory"]

    metadata = {}
    if params.category:
        metadata["category"] = params.category
    metadata["source"] = "manual"

    result = memory.add(content=params.content, metadata=metadata)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(
    name="wholemem_get_timeline",
    annotations={
        "title": "Get Activity Timeline",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wholemem_get_timeline(params: TimelineInput) -> str:
    """Get a chronological timeline of recent activity from Screenpipe.

    Returns timestamped entries showing what apps were used and what
    content was captured over the requested time window.

    Args:
        params: TimelineInput with minutes lookback window.

    Returns:
        JSON timeline of activity entries.
    """
    ctx = mcp.get_context()
    screenpipe: ScreenpipeClient = ctx.request_context.lifespan_context["screenpipe"]

    try:
        items = await screenpipe.get_recent_activity(minutes=params.minutes)
    except Exception as exc:
        return f"Error fetching timeline: {exc}"

    if not items:
        return "No activity found in the requested time window."

    timeline = []
    for item in items:
        ctype = item.get("type", "Unknown")
        content = item.get("content", {})
        entry = {
            "type": ctype,
            "timestamp": content.get("timestamp"),
        }
        if ctype == "OCR":
            entry["app"] = content.get("app_name")
            entry["snippet"] = content.get("text", "")[:200]
        elif ctype == "Audio":
            entry["snippet"] = content.get("transcription", "")[:200]
            entry["device"] = content.get("device_name")
        elif ctype == "UI":
            entry["app"] = content.get("app_name")
            entry["snippet"] = content.get("text", "")[:200]
        timeline.append(entry)

    return json.dumps(timeline, indent=2, default=str)


@mcp.tool(
    name="wholemem_get_daily_note",
    annotations={
        "title": "Read Obsidian Daily Note",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wholemem_get_daily_note(params: DailyNoteInput) -> str:
    """Read the Obsidian daily note for a given date.

    Returns the full Markdown content of the daily note, including
    all WholeMem sync entries appended throughout the day.

    Args:
        params: DailyNoteInput with optional date (default: today).

    Returns:
        Markdown content of the daily note.
    """
    ctx = mcp.get_context()
    obsidian: ObsidianWriter = ctx.request_context.lifespan_context["obsidian"]
    return obsidian.read_note(date_str=params.date)


@mcp.tool(
    name="wholemem_summarize_recent",
    annotations={
        "title": "Summarize Recent Activity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wholemem_summarize_recent(params: SummarizeInput) -> str:
    """Summarize recent screen and audio activity using the local SLM.

    Fetches the last N minutes of captures from Screenpipe and produces
    a concise summary using the configured small language model.

    Args:
        params: SummarizeInput with minutes lookback.

    Returns:
        Plain-text summary of recent activity.
    """
    ctx = mcp.get_context()
    screenpipe: ScreenpipeClient = ctx.request_context.lifespan_context["screenpipe"]
    summarizer: Summarizer = ctx.request_context.lifespan_context["summarizer"]

    try:
        items = await screenpipe.get_recent_activity(minutes=params.minutes)
    except Exception as exc:
        return f"Error fetching activity: {exc}"

    if not items:
        return "No activity found in the requested time window."

    try:
        summary = await summarizer.summarize_activity(items)
        return summary
    except Exception as exc:
        return f"Summarization failed: {exc}"


@mcp.tool(
    name="wholemem_status",
    annotations={
        "title": "System Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def wholemem_status() -> str:
    """Check the health of all WholeMem components.

    Reports availability of Screenpipe, LLM/summarizer, mem0, and Obsidian.

    Returns:
        JSON status report for each component.
    """
    ctx = mcp.get_context()
    state = ctx.request_context.lifespan_context
    screenpipe: ScreenpipeClient = state["screenpipe"]
    summarizer: Summarizer = state["summarizer"]
    memory: MemoryStore = state["memory"]
    obsidian: ObsidianWriter = state["obsidian"]
    config: WholeMemConfig = state["config"]

    status = {
        "screenpipe": {
            "url": config.screenpipe.url,
            "available": await screenpipe.is_available(),
        },
        "llm": {
            "base_url": config.llm.base_url,
            "model": config.llm.model,
            "available": await summarizer.is_available(),
        },
        "mem0": {
            "available": memory.is_available(),
            "user_id": config.mem0.user_id,
        },
        "obsidian": {
            "vault_path": config.obsidian.vault_path,
            "available": obsidian.is_available(),
        },
        "daemon": {
            "interval_minutes": config.daemon.interval_minutes,
        },
    }
    return json.dumps(status, indent=2)


@mcp.tool(
    name="wholemem_sync_now",
    annotations={
        "title": "Trigger Sync Now",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wholemem_sync_now() -> str:
    """Trigger an immediate sync cycle.

    Fetches recent Screenpipe captures, summarizes them, stores in mem0,
    and appends to today's Obsidian daily note — the same as the
    background daemon does every 15 minutes.

    Returns:
        Status message describing what was synced.
    """
    ctx = mcp.get_context()
    state = ctx.request_context.lifespan_context

    result = await trigger_sync(
        screenpipe=state["screenpipe"],
        summarizer=state["summarizer"],
        memory=state["memory"],
        obsidian=state["obsidian"],
        minutes=state["config"].daemon.interval_minutes,
    )
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the WholeMem MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
