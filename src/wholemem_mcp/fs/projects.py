"""Per-project datastore management.

Each immediate child directory of the watch root is a *project* with its own
SQLite database (versions + blobs). Loose files at the root belong to the
catch-all ``_root`` project. Paths stored in every DB remain relative to the
watch root, so exclude patterns, tool ``path`` arguments, and migration are
unaffected by the sharding.

Sessions stay global (agents span projects) in a separate shared DB owned by
the service.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from wholemem_mcp.fs.blob_store import BlobStore
from wholemem_mcp.fs.version_store import VersionEntry, VersionStore

log = logging.getLogger(__name__)

ROOT_PROJECT = "_root"


def db_filename(root: Path, project: str) -> str:
    """Stable per-project DB filename (mirrors the old wawd convention)."""
    ws_path = str((root / project).resolve()) if project != ROOT_PROJECT else str(root.resolve())
    slug = project if project != ROOT_PROJECT else f"{root.name}{ROOT_PROJECT}"
    ws_hash = hashlib.sha1(ws_path.encode()).hexdigest()[:8]
    return f"wholemem_{slug}_{ws_hash}.db"


def sessions_db_filename(root: Path) -> str:
    ws_hash = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:8]
    return f"wholemem_sessions_{ws_hash}.db"


@dataclass
class StoreHandle:
    """An open per-project datastore."""

    project: str
    path: Path  # absolute project directory
    db: aiosqlite.Connection
    blob_store: BlobStore
    version_store: VersionStore
    # Slots the service fills in lazily (oracle wiring):
    runtime: Dict[str, Any] = field(default_factory=dict)


class ProjectStores:
    """Lazily opens and caches one datastore per project directory."""

    def __init__(
        self,
        root: str | Path,
        db_dir: str | Path,
        compression_level: int = 3,
        history_depth: int = 3,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.db_dir = Path(db_dir).expanduser()
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._compression = compression_level
        self._history_depth = history_depth
        self._handles: Dict[str, StoreHandle] = {}
        self._lock = asyncio.Lock()

    # ── project resolution ────────────────────────────────────────

    def project_for_rel(self, rel_path: str) -> str:
        """Project owning a root-relative path: its top-level dir, or _root."""
        rel = rel_path.replace("\\", "/").lstrip("/")
        if "/" in rel:
            return rel.split("/", 1)[0]
        return ROOT_PROJECT

    def project_for_workspace(self, workspace: str | Path | None) -> Optional[str]:
        """Map an absolute workspace path to a project name.

        Returns None when the workspace IS the watch root (aggregate scope)
        or lies outside it.
        """
        if workspace is None:
            return None
        ws = Path(workspace).expanduser().resolve()
        if ws == self.root:
            return None
        try:
            rel = ws.relative_to(self.root)
        except ValueError:
            log.warning("Workspace %s is outside watch root %s", ws, self.root)
            return None
        return rel.parts[0]

    def project_path(self, project: str) -> Path:
        return self.root if project == ROOT_PROJECT else self.root / project

    def known_projects(self) -> List[str]:
        """Projects that have a DB on disk or are currently open."""
        names = set(self._handles)
        prefix = "wholemem_"
        for f in self.db_dir.glob("wholemem_*.db"):
            stem = f.stem[len(prefix):]
            name = stem.rsplit("_", 1)[0]
            if name == f"{self.root.name}{ROOT_PROJECT}":
                name = ROOT_PROJECT
            if name == self.root.name:
                # legacy monolithic DB (pre-split) — handled by wholemem-migrate
                continue
            if name != "sessions" and not name.startswith("sessions_"):
                names.add(name)
        # plus any current top-level dirs (so aggregate queries see new projects)
        if self.root.is_dir():
            for child in self.root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    names.add(child.name)
        return sorted(names)

    # ── handle lifecycle ──────────────────────────────────────────

    async def get(self, project: str) -> StoreHandle:
        """Open (or fetch the cached) datastore for *project*."""
        handle = self._handles.get(project)
        if handle is not None:
            return handle
        async with self._lock:
            handle = self._handles.get(project)
            if handle is not None:
                return handle
            db_path = self.db_dir / db_filename(self.root, project)
            db = await aiosqlite.connect(str(db_path))
            blobs = BlobStore(db, self._compression)
            await blobs.init_db()
            versions = VersionStore(db, blobs, history_depth=self._history_depth)
            await versions.init_db()
            handle = StoreHandle(
                project=project,
                path=self.project_path(project),
                db=db,
                blob_store=blobs,
                version_store=versions,
            )
            self._handles[project] = handle
            log.info("Opened project store '%s' (%s)", project, db_path.name)
            return handle

    async def get_for_rel(self, rel_path: str) -> StoreHandle:
        return await self.get(self.project_for_rel(rel_path))

    async def store_for_rel(self, rel_path: str) -> VersionStore:
        """StoreResolver for the watcher."""
        return (await self.get_for_rel(rel_path)).version_store

    def open_handles(self) -> List[StoreHandle]:
        return list(self._handles.values())

    async def close_all(self) -> None:
        for handle in self._handles.values():
            try:
                await handle.db.close()
            except Exception:
                log.exception("Error closing store for %s", handle.project)
        self._handles.clear()

    # ── aggregate reads (across all known projects) ───────────────

    async def _all_handles(self) -> List[StoreHandle]:
        return [await self.get(p) for p in self.known_projects()] or (
            [await self.get(ROOT_PROJECT)]
        )

    async def get_changes_since(self, timestamp: float) -> List[VersionEntry]:
        results: List[VersionEntry] = []
        for handle in await self._all_handles():
            results.extend(await handle.version_store.get_changes_since(timestamp))
        return sorted(results, key=lambda e: e.timestamp, reverse=True)

    async def get_changes_by_agent(
        self, agent_id: str, since: float | None = None
    ) -> List[VersionEntry]:
        results: List[VersionEntry] = []
        for handle in await self._all_handles():
            results.extend(
                await handle.version_store.get_changes_by_agent(agent_id, since=since)
            )
        return sorted(results, key=lambda e: e.timestamp, reverse=True)

    async def get_history(
        self, path: str, limit: int = 50, since_timestamp: float | None = None
    ) -> List[VersionEntry]:
        handle = await self.get_for_rel(path)
        return await handle.version_store.get_history(
            path, limit=limit, since_timestamp=since_timestamp
        )
