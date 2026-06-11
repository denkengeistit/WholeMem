"""Split a monolithic WholeMem version DB into per-project databases.

Offline migration — STOP the WholeMem service first.

  wholemem-migrate            # dry run by default: prints the plan
  wholemem-migrate --execute  # perform the split, archive old DB as .bak

What it does:
  * partitions `versions` rows by project (top-level dir under the watch
    root; loose root files → `_root`)
  * drops rows matching current `watcher.exclude` patterns (the big GC win)
  * remaps `parent_version_id` chains into the new per-project id space
  * copies only the blobs still referenced
  * moves `sessions` into the new shared sessions DB
  * archives the monolith as `<name>.db.bak`

Snapshots are NOT migrated (they reference cross-project version ids);
the old DB remains in the .bak archive if you ever need one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from wholemem_mcp.fs.exclude import is_excluded
from wholemem_mcp.fs.projects import ROOT_PROJECT, db_filename, sessions_db_filename

VERSIONS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    blob_hash TEXT REFERENCES blobs(hash),
    operation TEXT NOT NULL CHECK(operation IN ('create', 'modify', 'delete')),
    agent_id TEXT,
    session_id TEXT,
    timestamp REAL NOT NULL DEFAULT (unixepoch('subsec')),
    intent TEXT,
    parent_version_id INTEGER REFERENCES versions(id)
);
CREATE INDEX IF NOT EXISTS idx_versions_path ON versions(path, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_versions_session ON versions(session_id);
CREATE INDEX IF NOT EXISTS idx_versions_agent ON versions(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_versions_timestamp ON versions(timestamp DESC);
CREATE TABLE IF NOT EXISTS snapshots (
    name TEXT PRIMARY KEY,
    description TEXT,
    timestamp REAL NOT NULL DEFAULT (unixepoch('subsec')),
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_files (
    snapshot_name TEXT REFERENCES snapshots(name) ON DELETE CASCADE,
    path TEXT NOT NULL,
    version_id INTEGER REFERENCES versions(id),
    PRIMARY KEY (snapshot_name, path)
);
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    compressed BLOB NOT NULL
);
"""

SESSIONS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    task TEXT,
    started_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed')),
    summary TEXT
);
"""


def _project_for(rel_path: str) -> str:
    rel = rel_path.replace("\\", "/").lstrip("/")
    return rel.split("/", 1)[0] if "/" in rel else ROOT_PROJECT


def _old_db_path(cfg) -> Path:
    import hashlib

    ws_path = str(Path(cfg.watcher.path).expanduser().resolve())
    slug = Path(ws_path).name
    ws_hash = hashlib.sha1(ws_path.encode()).hexdigest()[:8]
    return Path(cfg.versioning.db_path).expanduser() / f"wholemem_{slug}_{ws_hash}.db"


def migrate(execute: bool = False, cfg=None) -> int:
    if cfg is None:
        from wholemem_mcp.config import load_config
        cfg = load_config()
    root = Path(cfg.watcher.path).expanduser().resolve()
    db_dir = Path(cfg.versioning.db_path).expanduser()
    exclude = cfg.watcher.exclude

    old_path = _old_db_path(cfg)
    if not old_path.exists():
        print(f"Nothing to migrate: {old_path} not found.")
        return 0

    print(f"Source:  {old_path} ({old_path.stat().st_size / 1e6:.1f} MB)")
    print(f"Root:    {root}")
    print(f"Mode:    {'EXECUTE' if execute else 'DRY RUN (use --execute to apply)'}\n")

    src = sqlite3.connect(str(old_path))
    src.row_factory = sqlite3.Row

    # ── pass 1: plan ──────────────────────────────────────────────
    t0 = time.time()
    counts: dict[str, int] = {}
    skipped = 0
    for row in src.execute("SELECT path FROM versions"):
        if is_excluded(row["path"], exclude):
            skipped += 1
        else:
            project = _project_for(row["path"])
            counts[project] = counts.get(project, 0) + 1

    total_kept = sum(counts.values())
    print(f"{total_kept + skipped} versions total → keep {total_kept}, drop {skipped} (excluded)")
    for project, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {project:30s} {n:8d} versions → {db_filename(root, project)}")

    n_sessions = src.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] \
        if src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone() else 0
    print(f"  {'(sessions)':30s} {n_sessions:8d} rows     → {sessions_db_filename(root)}")

    if not execute:
        src.close()
        print(f"\nPlanned in {time.time() - t0:.1f}s. Re-run with --execute to apply.")
        return 0

    # ── pass 2: execute ───────────────────────────────────────────
    dests: dict[str, sqlite3.Connection] = {}
    id_maps: dict[str, dict[int, int]] = {}
    blob_seen: dict[str, set[str]] = {}

    def dest_for(project: str) -> sqlite3.Connection:
        conn = dests.get(project)
        if conn is None:
            p = db_dir / db_filename(root, project)
            conn = sqlite3.connect(str(p))
            conn.executescript("PRAGMA journal_mode=WAL;\nPRAGMA synchronous=NORMAL;")
            conn.executescript(VERSIONS_SCHEMA)
            dests[project] = conn
            id_maps[project] = {}
            blob_seen[project] = set()
        return conn

    migrated = 0
    for row in src.execute(
        """SELECT id, path, blob_hash, operation, agent_id, session_id,
                  timestamp, intent, parent_version_id
           FROM versions ORDER BY id ASC"""
    ):
        if is_excluded(row["path"], exclude):
            continue
        project = _project_for(row["path"])
        dst = dest_for(project)
        id_map = id_maps[project]

        # copy referenced blob once per destination
        bh = row["blob_hash"]
        if bh and bh not in blob_seen[project]:
            blob = src.execute(
                "SELECT hash, size, compressed FROM blobs WHERE hash = ?", (bh,)
            ).fetchone()
            if blob:
                dst.execute(
                    "INSERT OR IGNORE INTO blobs (hash, size, compressed) VALUES (?, ?, ?)",
                    (blob["hash"], blob["size"], blob["compressed"]),
                )
            blob_seen[project].add(bh)

        parent = row["parent_version_id"]
        new_parent = id_map.get(parent) if parent is not None else None
        cur = dst.execute(
            """INSERT INTO versions
               (path, blob_hash, operation, agent_id, session_id, timestamp, intent, parent_version_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["path"], bh, row["operation"], row["agent_id"],
             row["session_id"], row["timestamp"], row["intent"], new_parent),
        )
        id_map[row["id"]] = cur.lastrowid
        migrated += 1
        if migrated % 50000 == 0:
            print(f"  ... {migrated}/{total_kept}")
            for c in dests.values():
                c.commit()

    for project, conn in dests.items():
        conn.commit()
        conn.execute("VACUUM")
        conn.close()

    # sessions → shared DB
    if n_sessions:
        sess = sqlite3.connect(str(db_dir / sessions_db_filename(root)))
        sess.executescript(SESSIONS_SCHEMA)
        for row in src.execute("SELECT id, agent_name, task, started_at, last_seen_at, status, summary FROM sessions"):
            sess.execute(
                "INSERT OR IGNORE INTO sessions (id, agent_name, task, started_at, last_seen_at, status, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )
        sess.commit()
        sess.close()

    src.close()

    bak = old_path.with_suffix(".db.bak")
    old_path.rename(bak)
    # WAL sidecars too
    for ext in ("-wal", "-shm"):
        side = Path(str(old_path) + ext)
        if side.exists():
            side.rename(str(bak) + ext)

    print(f"\nDone in {time.time() - t0:.1f}s: {migrated} versions across "
          f"{len(dests)} project DBs, {n_sessions} sessions moved.")
    print(f"Old DB archived: {bak}")
    print("Restart the WholeMem service now.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Apply the migration (default is a dry run)")
    args = parser.parse_args()
    sys.exit(migrate(execute=args.execute))


if __name__ == "__main__":
    main()
