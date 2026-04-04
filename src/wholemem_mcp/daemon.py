"""Background daemon — runs a sync loop every N minutes.

1. Fetch recent activity from Screenpipe
2. Summarize via SLM
3. Store summaries in mem0
4. Append to today's Obsidian Daily Note
5. Query version store for file changes, summarize, store in mem0
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from wholemem_mcp.config import WholeMemConfig
from wholemem_mcp.fs.version_store import VersionStore
from wholemem_mcp.memory import MemoryStore
from wholemem_mcp.obsidian import ObsidianWriter
from wholemem_mcp.screenpipe import ScreenpipeClient
from wholemem_mcp.summarizer import Summarizer

logger = logging.getLogger("wholemem.daemon")


async def _run_sync_cycle(
    screenpipe: ScreenpipeClient,
    summarizer: Summarizer,
    memory: MemoryStore,
    obsidian: ObsidianWriter,
    interval_minutes: int,
    version_store: VersionStore | None = None,
) -> str:
    """Execute a single sync cycle.

    Returns:
        A status message describing what was done.
    """
    # 1. Fetch recent activity -------------------------------------------
    # Fetch a wider window than the interval to account for Screenpipe
    # indexing lag. Dedup in _flatten_items handles any overlap.
    fetch_minutes = max(interval_minutes * 2, 2)
    try:
        items = await screenpipe.get_recent_activity(minutes=fetch_minutes)
    except Exception as exc:
        msg = f"Screenpipe fetch failed: {exc}"
        logger.warning(msg)
        return msg

    if not items:
        logger.info("No new activity from Screenpipe.")
        return "No new activity."

    logger.info("Fetched %d items from Screenpipe.", len(items))

    # 2. Summarize -------------------------------------------------------
    try:
        summary_text = await summarizer.summarize_activity(items)
        note_md = await summarizer.summarize_for_daily_note(items)
    except Exception as exc:
        # If summarizer is unavailable, fall back to raw text storage
        logger.warning("Summarizer failed (%s), storing raw metadata.", exc)
        summary_text = f"Raw activity: {len(items)} items captured."
        note_md = f"- {len(items)} items captured (summarizer unavailable)\n"

    # 3. Store in mem0 ---------------------------------------------------
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        memory.add(
            content=summary_text,
            metadata={
                "source": "wholemem_daemon",
                "timestamp": timestamp,
                "item_count": len(items),
            },
        )
        logger.info("Stored summary in mem0.")
    except Exception as exc:
        logger.warning("mem0 storage failed: %s", exc)

    # 4. Append to Obsidian Daily Note -----------------------------------
    try:
        path = obsidian.append_entry(note_md)
        logger.info("Appended to daily note: %s", path)
    except Exception as exc:
        logger.warning("Obsidian write failed: %s", exc)

    # 5. File change sync ------------------------------------------------
    if version_store is not None:
        try:
            import time
            since = time.time() - (interval_minutes * 60)
            changes = await version_store.get_changes_since(since)
            if changes:
                change_lines = []
                for c in changes:
                    change_lines.append(f"{c.operation} {c.path} by {c.agent_id or 'unknown'}")
                change_text = f"File changes ({len(changes)}): " + "; ".join(change_lines[:20])
                memory.add(
                    content=change_text,
                    metadata={
                        "source": "file_watcher",
                        "timestamp": timestamp,
                        "change_count": len(changes),
                    },
                )
                logger.info("Stored %d file changes in mem0.", len(changes))
        except Exception as exc:
            logger.warning("File change sync failed: %s", exc)

    return f"Synced {len(items)} items. Summary stored and daily note updated."


async def run_daemon(
    config: WholeMemConfig,
    screenpipe: ScreenpipeClient,
    summarizer: Summarizer,
    memory: MemoryStore,
    obsidian: ObsidianWriter,
    version_store: VersionStore | None = None,
) -> None:
    """Run the background sync loop forever.

    This is meant to be started as an asyncio task and cancelled on shutdown.
    """
    interval = config.daemon.interval_minutes * 60  # seconds
    logger.info(
        "WholeMem daemon started — syncing every %d minutes.",
        config.daemon.interval_minutes,
    )

    while True:
        try:
            status = await _run_sync_cycle(
                screenpipe, summarizer, memory, obsidian,
                config.daemon.interval_minutes,
                version_store=version_store,
            )
            logger.info("Sync cycle complete: %s", status)
        except Exception as exc:
            logger.error("Unexpected error in sync cycle: %s", exc, exc_info=True)

        await asyncio.sleep(interval)


async def trigger_sync(
    screenpipe: ScreenpipeClient,
    summarizer: Summarizer,
    memory: MemoryStore,
    obsidian: ObsidianWriter,
    minutes: int = 15,
    version_store: VersionStore | None = None,
) -> str:
    """Manually trigger a single sync cycle (used internally)."""
    return await _run_sync_cycle(
        screenpipe, summarizer, memory, obsidian, minutes,
        version_store=version_store,
    )
