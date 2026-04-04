"""WholeMemService — core service that owns all shared state.

This is the long-lived singleton that starts/stops all components:
Screenpipe, mem0/Qdrant, watcher, version store, oracle, daemon.

Both the MCP server and the Streamlit UI access it — the MCP server
directly (in-process), the UI via HTTP endpoints on the server.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

from wholemem_mcp.config import WholeMemConfig, load_config
from wholemem_mcp.daemon import run_daemon
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

logger = logging.getLogger("wholemem.service")


def _db_path_for_workspace(config: WholeMemConfig) -> Path:
    """Derive a unique DB path per workspace (matches wawd convention)."""
    ws_path = str(Path(config.watcher.path).expanduser().resolve())
    slug = Path(ws_path).name
    ws_hash = hashlib.sha1(ws_path.encode()).hexdigest()[:8]
    db_dir = Path(config.versioning.db_path).expanduser()
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"wholemem_{slug}_{ws_hash}.db"


class WholeMemService:
    """Owns all WholeMem components. Start once, share everywhere."""

    def __init__(self, config: WholeMemConfig) -> None:
        self.config = config
        self._started_at: float | None = None

        # Components — populated by start()
        self.screenpipe_client: ScreenpipeClient | None = None
        self.summarizer: Summarizer | None = None
        self.memory: MemoryStore | None = None
        self.obsidian: ObsidianWriter | None = None
        self.sp_process: ScreenpipeProcess | None = None
        self.db: aiosqlite.Connection | None = None
        self.blob_store: BlobStore | None = None
        self.version_store: VersionStore | None = None
        self.session_tracker: SessionTracker | None = None
        self.watcher: WAWDWatcher | None = None
        self.oracle: Oracle | None = None
        self.backend: OpenAICompatBackend | None = None
        self.task_store: TaskStore | None = None
        self._daemon_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all components."""
        cfg = self.config
        self._started_at = time.time()

        # --- Screenpipe ---
        if cfg.screenpipe.managed:
            self.sp_process = ScreenpipeProcess(cfg.screenpipe)
            try:
                await self.sp_process.start()
            except Exception as exc:
                logger.warning("Failed to start managed Screenpipe: %s", exc)
                self.sp_process = None

        self.screenpipe_client = ScreenpipeClient(cfg.screenpipe)
        self.summarizer = Summarizer(cfg.summarizer_llm, cfg.llm)
        self.memory = MemoryStore(cfg)
        self.obsidian = ObsidianWriter(cfg.obsidian)

        # --- Version store (SQLite + watchdog) ---
        if cfg.watcher.enabled:
            db_path = _db_path_for_workspace(cfg)
            self.db = await aiosqlite.connect(str(db_path))

            self.blob_store = BlobStore(self.db, cfg.versioning.compression_level)
            await self.blob_store.init_db()
            self.version_store = VersionStore(self.db, self.blob_store)
            await self.version_store.init_db()
            self.session_tracker = SessionTracker(
                self.db, cfg.oracle.session_timeout_minutes,
            )
            await self.session_tracker.init_db()

            # Oracle backend — reuses the LLM config
            self.backend = OpenAICompatBackend(
                base_url=cfg.llm.base_url,
                model=cfg.llm.model,
                api_key=cfg.llm.api_key,
                timeout=300.0,
            )

            context_builder = ContextBuilder(
                self.version_store,
                self.session_tracker,
                cfg.oracle.history_depth,
            )
            restorer = Restorer(
                self.version_store,
                context_builder,
                self.backend,
                str(Path(cfg.watcher.path).expanduser()),
            )
            self.oracle = Oracle(
                self.version_store,
                self.session_tracker,
                context_builder,
                restorer,
                self.backend,
                str(Path(cfg.watcher.path).expanduser()),
            )

            # Start watcher
            ws_path = str(Path(cfg.watcher.path).expanduser())
            self.watcher = WAWDWatcher(
                ws_path,
                self.version_store,
                cfg.watcher.exclude,
                session_tracker=self.session_tracker,
            )
            await self.watcher.start()
            self.oracle.set_watcher(self.watcher)
            restorer.set_watcher(self.watcher)

            logger.info("Watcher + oracle started for %s", ws_path)

        # --- Task store ---
        if cfg.watcher.enabled:
            self.task_store = TaskStore(Path(cfg.watcher.path).expanduser())

        # --- Background daemon ---
        self._daemon_task = asyncio.create_task(
            run_daemon(
                cfg,
                self.screenpipe_client,
                self.summarizer,
                self.memory,
                self.obsidian,
                version_store=self.version_store,
            )
        )

        logger.info("WholeMemService started.")

    async def stop(self) -> None:
        """Tear down all components. Each step is isolated so failures
        don't prevent subsequent cleanup."""

        # 1. Cancel daemon
        if self._daemon_task:
            self._daemon_task.cancel()
            try:
                await self._daemon_task
            except asyncio.CancelledError:
                pass
            logger.info("Daemon stopped.")

        # 2. Stop watcher (flushes pending, joins observer with timeout)
        if self.watcher:
            try:
                await self.watcher.stop()
            except Exception:
                logger.exception("Error stopping watcher")

        # 3. Close oracle backend
        if self.backend:
            try:
                await self.backend.close()
            except Exception:
                logger.exception("Error closing backend")

        # 4. End active sessions before closing DB
        if self.session_tracker:
            try:
                active = await self.session_tracker.get_active_sessions()
                for s in active:
                    await self.session_tracker._complete_session(s.id)
                if active:
                    logger.info("Ended %d active session(s).", len(active))
            except Exception:
                logger.exception("Error ending sessions")

        # 5. Close mem0 / Qdrant
        if self.memory:
            try:
                self.memory.close()
            except Exception:
                logger.exception("Error closing memory store")

        # 6. Close SQLite
        if self.db:
            try:
                await self.db.close()
            except Exception:
                logger.exception("Error closing database")

        # 7. Stop managed Screenpipe
        if self.sp_process is not None:
            try:
                await self.sp_process.stop()
            except Exception:
                logger.exception("Error stopping Screenpipe")

        logger.info("WholeMemService shutdown complete.")

    # ------------------------------------------------------------------
    # Status / health
    # ------------------------------------------------------------------

    async def status(self) -> Dict[str, Any]:
        """Component health check — used by /health endpoint."""
        result: Dict[str, Any] = {
            "uptime_seconds": round(time.time() - self._started_at, 1)
            if self._started_at
            else 0,
        }

        # Screenpipe
        try:
            sp_ok = await self.screenpipe_client.is_available() if self.screenpipe_client else False
        except Exception:
            sp_ok = False
        result["screenpipe"] = {"available": sp_ok, "managed": self.config.screenpipe.managed}

        # LLM
        try:
            llm_ok = await self.summarizer.is_available() if self.summarizer else False
        except Exception:
            llm_ok = False
        result["llm"] = {"available": llm_ok, "model": self.config.llm.model}

        # mem0
        result["mem0"] = {"available": self.memory.is_available() if self.memory else False}

        # Obsidian
        result["obsidian"] = {"available": self.obsidian.is_available() if self.obsidian else False}

        # Watcher
        result["watcher"] = {"enabled": self.config.watcher.enabled, "path": self.config.watcher.path}

        # Sessions
        if self.session_tracker:
            try:
                active = await self.session_tracker.get_active_sessions()
                result["sessions"] = {
                    "active": [
                        {
                            "agent": s.agent_name,
                            "task": s.task,
                            "started_at": s.started_at,
                            "last_seen_at": s.last_seen_at,
                        }
                        for s in active
                    ]
                }
            except Exception:
                result["sessions"] = {"active": []}
        else:
            result["sessions"] = {"active": []}

        return result

    # ------------------------------------------------------------------
    # Context dict — for MCP lifespan compatibility
    # ------------------------------------------------------------------

    def context_dict(self) -> Dict[str, Any]:
        """Return the context dict that MCP tool handlers expect."""
        return {
            "config": self.config,
            "screenpipe": self.screenpipe_client,
            "summarizer": self.summarizer,
            "memory": self.memory,
            "obsidian": self.obsidian,
            "daemon_task": self._daemon_task,
            "db": self.db,
            "blob_store": self.blob_store,
            "version_store": self.version_store,
            "session_tracker": self.session_tracker,
            "watcher": self.watcher,
            "oracle": self.oracle,
            "backend": self.backend,
            "task_store": self.task_store,
        }
