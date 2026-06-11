"""Garbage collection for per-project version stores.

Three sweeps, run periodically by the daemon (and available on demand):

1. **Excluded paths** — versions whose path matches the current
   ``watcher.exclude`` patterns (covers patterns added after the fact).
2. **Tombstones** — files whose latest operation is ``delete`` and whose
   deletion is older than the retention window: the entire chain is purged.
   Until then the last content version remains restorable via ``fix_this``.
3. **Orphan blobs** — blobs no longer referenced by any version, then a
   VACUUM when enough rows were freed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from wholemem_mcp.fs.exclude import is_excluded
from wholemem_mcp.fs.projects import ProjectStores, StoreHandle

log = logging.getLogger(__name__)


@dataclass
class GCStats:
    project: str = ""
    excluded_paths: int = 0
    tombstoned_paths: int = 0
    versions_deleted: int = 0
    blobs_deleted: int = 0
    vacuumed: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "project": self.project,
            "excluded_paths": self.excluded_paths,
            "tombstoned_paths": self.tombstoned_paths,
            "versions_deleted": self.versions_deleted,
            "blobs_deleted": self.blobs_deleted,
            "vacuumed": self.vacuumed,
        }


async def gc_handle(
    handle: StoreHandle,
    exclude: Sequence[str],
    retention_days: int = 30,
    vacuum_threshold: int = 500,
) -> GCStats:
    """Run all GC sweeps against one project datastore."""
    db = handle.db
    stats = GCStats(project=handle.project)

    # ── 1. excluded paths ─────────────────────────────────────────
    cursor = await db.execute("SELECT DISTINCT path FROM versions")
    all_paths = [r[0] for r in await cursor.fetchall()]
    excluded = [p for p in all_paths if is_excluded(p, exclude)]
    for path in excluded:
        cursor = await db.execute(
            "DELETE FROM versions WHERE path = ?", (path,)
        )
        stats.versions_deleted += cursor.rowcount or 0
    stats.excluded_paths = len(excluded)

    # ── 2. tombstones past retention ──────────────────────────────
    cutoff = time.time() - retention_days * 86400
    cursor = await db.execute(
        """WITH latest AS (
             SELECT path, operation, timestamp,
                    ROW_NUMBER() OVER (PARTITION BY path ORDER BY timestamp DESC) AS rn
             FROM versions
           )
           SELECT path FROM latest
           WHERE rn = 1 AND operation = 'delete' AND timestamp < ?""",
        (cutoff,),
    )
    dead_paths = [r[0] for r in await cursor.fetchall()]
    for path in dead_paths:
        cursor = await db.execute(
            "DELETE FROM versions WHERE path = ?", (path,)
        )
        stats.versions_deleted += cursor.rowcount or 0
    stats.tombstoned_paths = len(dead_paths)

    # also purge dangling snapshot references
    await db.execute(
        "DELETE FROM snapshot_files WHERE version_id NOT IN (SELECT id FROM versions)"
    )

    # ── 3. orphan blobs + vacuum ──────────────────────────────────
    cursor = await db.execute(
        """DELETE FROM blobs WHERE hash NOT IN (
             SELECT DISTINCT blob_hash FROM versions WHERE blob_hash IS NOT NULL
           )"""
    )
    stats.blobs_deleted = cursor.rowcount or 0

    await db.commit()

    if stats.versions_deleted + stats.blobs_deleted >= vacuum_threshold:
        try:
            await db.execute("VACUUM")
            stats.vacuumed = True
        except Exception:
            log.warning("VACUUM failed for project %s", handle.project, exc_info=True)

    if stats.versions_deleted or stats.blobs_deleted:
        log.info(
            "GC %s: %d versions (%d excluded paths, %d tombstoned paths), "
            "%d blobs%s",
            handle.project,
            stats.versions_deleted,
            stats.excluded_paths,
            stats.tombstoned_paths,
            stats.blobs_deleted,
            ", vacuumed" if stats.vacuumed else "",
        )
    return stats


async def gc_all(
    projects: ProjectStores,
    exclude: Sequence[str],
    retention_days: int = 30,
    vacuum_threshold: int = 500,
) -> List[GCStats]:
    """Run GC across every known project. Returns per-project stats."""
    results: List[GCStats] = []
    for name in projects.known_projects():
        handle = await projects.get(name)
        try:
            results.append(
                await gc_handle(handle, exclude, retention_days, vacuum_threshold)
            )
        except Exception:
            log.exception("GC failed for project %s", name)
    return results
