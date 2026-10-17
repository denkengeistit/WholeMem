#!/usr/bin/env python3
"""ledger: a minimal artifact ledger for agent work (stripped from WholeMem's watcher).

Watches ~/tasks/<card>/ (one workdir per Vikunja card, made by dispatch.py) and
records every file create/write/delete in SQLite, with deduplicated, compressed
content, so anyone can ask what an agent actually changed.

  ledger.py watch                      run the watcher (systemd)
  ledger.py log [card] [--since H] [--limit N]   recent changes as JSON
  ledger.py diff <change_id>           unified diff vs the previous version
  ledger.py show <change_id>           file content at that change
  ledger.py restore <change_id>        put the file back as it was at that change (CLI only)
  ledger.py mcp                        read-only stdio MCP server (for ZeroClaw)

Readers open the DB read-only. restore is CLI only and never exposed over MCP; the
watcher logs the restore as a new change, so a restore can itself be undone.
"""
import difflib, hashlib, json, os, sqlite3, sys, threading, time, zlib

HOME = os.path.expanduser("~")
ROOT = os.environ.get("LEDGER_ROOT", f"{HOME}/tasks")
DB = os.environ.get("LEDGER_DB", f"{HOME}/ledger/ledger.db")
RUNS = f"{HOME}/watcher/runs"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".mypy_cache", ".pytest_cache", "dist", "build"}
MAX_BYTES = 2_000_000
DEBOUNCE = 1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, ts REAL, card INTEGER, agent TEXT,
  path TEXT, op TEXT, sha TEXT, size INTEGER);
CREATE INDEX IF NOT EXISTS ix_card ON changes(card, id);
CREATE INDEX IF NOT EXISTS ix_path ON changes(card, path, id);
"""


def rw():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False)
    c.executescript(SCHEMA)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def split(abs_path):
    """~/tasks/<card>/<rel> -> (card, rel) or None."""
    rel = os.path.relpath(abs_path, ROOT)
    parts = rel.split(os.sep)
    if len(parts) < 2 or not parts[0].isdigit() or any(p in SKIP_DIRS for p in parts[:-1]):
        return None
    if parts[-1].endswith((".swp", ".tmp", "~", ".ledger-restore")) or parts[-1].startswith(".#"):
        return None
    return int(parts[0]), os.sep.join(parts[1:])


def agent_for(card):
    try:
        return json.load(open(f"{RUNS}/{card}.json")).get("agent")
    except Exception:
        return None


def last_sha(db, card, path):
    r = db.execute("SELECT sha FROM changes WHERE card=? AND path=? ORDER BY id DESC LIMIT 1", (card, path)).fetchone()
    return r[0] if r else None


def record(db, abs_path, op):
    cp = split(abs_path)
    if not cp:
        return
    card, path = cp
    sha, size = None, None
    if op != "delete":
        if not os.path.isfile(abs_path):
            op = "delete"
        else:
            size = os.path.getsize(abs_path)
            if size > MAX_BYTES:
                sha = "too-large"
            else:
                data = open(abs_path, "rb").read()
                sha = hashlib.sha256(data).hexdigest()
                db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (sha, zlib.compress(data, 6)))
    prev = last_sha(db, card, path)
    if op == "delete" and prev is None:
        return
    if op != "delete" and sha == prev:
        return  # touched, not changed
    if op != "delete":
        op = "create" if prev is None else "write"
    db.execute("INSERT INTO changes(ts,card,agent,path,op,sha,size) VALUES(?,?,?,?,?,?,?)",
               (time.time(), card, agent_for(card), path, op, sha, size))
    db.commit()


def watch():
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    os.makedirs(ROOT, exist_ok=True)
    db, pending, lock = rw(), {}, threading.Lock()

    class H(FileSystemEventHandler):
        def on_any_event(self, e):
            if e.is_directory:
                return
            with lock:
                if e.event_type == "moved":
                    pending[e.src_path] = "delete"; pending[e.dest_path] = "write"
                elif e.event_type in ("created", "modified", "closed"):
                    pending[e.src_path] = "write"
                elif e.event_type == "deleted":
                    pending[e.src_path] = "delete"

    # initial pass: catch anything changed while we were down
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            record(db, os.path.join(d, f), "write")
    obs = Observer(); obs.schedule(H(), ROOT, recursive=True); obs.start()
    print(f"ledger watching {ROOT} -> {DB}", flush=True)
    while True:
        time.sleep(DEBOUNCE)
        with lock:
            batch = dict(pending); pending.clear()
        for p, op in batch.items():
            try:
                record(db, p, op)
            except Exception as ex:
                print(f"error {p}: {ex}", flush=True)


def blob(db, sha):
    if not sha or sha == "too-large":
        return None
    r = db.execute("SELECT data FROM blobs WHERE sha=?", (sha,)).fetchone()
    return zlib.decompress(r[0]) if r else None


def q_log(card=None, since_hours=24, limit=50):
    db = ro()
    sql = "SELECT id,ts,card,agent,path,op,size FROM changes WHERE ts>=?"
    args = [time.time() - float(since_hours) * 3600]
    if card is not None:
        sql += " AND card=?"; args.append(int(card))
    sql += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
    return [{"id": i, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), "card": c, "agent": a,
             "path": p, "op": o, "size": s} for i, t, c, a, p, o, s in db.execute(sql, args)]


def q_row(db, cid):
    r = db.execute("SELECT card,path,sha,op FROM changes WHERE id=?", (int(cid),)).fetchone()
    if not r:
        raise ValueError(f"no change {cid}")
    return r


def q_show(cid):
    db = ro(); card, path, sha, op = q_row(db, cid)
    if op == "delete":
        return f"(deleted {path})"
    b = blob(db, sha)
    return "(file over 2 MB, not stored)" if b is None else b.decode("utf-8", "replace")


def restore(cid):
    db = ro(); card, path, sha, op = q_row(db, cid)
    dest = os.path.realpath(os.path.join(ROOT, str(card), path))
    if not dest.startswith(os.path.realpath(os.path.join(ROOT, str(card))) + os.sep):
        raise ValueError("path escapes card workdir")
    if op == "delete":
        if os.path.exists(dest):
            os.remove(dest)
        return f"removed {dest} (state at change {cid} was deleted)"
    b = blob(db, sha)
    if b is None:
        raise ValueError("content not stored (file over 2 MB)")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".ledger-restore"
    with open(tmp, "wb") as f:
        f.write(b)
    os.replace(tmp, dest)
    return f"restored {dest} to change {cid} ({len(b)} bytes)"


def q_diff(cid):
    db = ro(); card, path, sha, op = q_row(db, cid)
    prev = db.execute("SELECT sha FROM changes WHERE card=? AND path=? AND id<? ORDER BY id DESC LIMIT 1",
                      (card, path, int(cid))).fetchone()
    a = blob(db, prev[0]) if prev else b""
    b = b"" if op == "delete" else blob(db, sha)
    if a is None or b is None:
        return "(file over 2 MB, no diff)"
    d = difflib.unified_diff(a.decode("utf-8", "replace").splitlines(True), b.decode("utf-8", "replace").splitlines(True),
                             f"a/{path}", f"b/{path}")
    out = "".join(d)
    return out[:20000] or "(no text difference)"


TOOLS = [
    {"name": "ledger_log", "description": "Recent file changes agents made in their card workdirs (~/tasks/<card>). Newest first. Use it to see what a worker actually did on a card.",
     "inputSchema": {"type": "object", "properties": {"card": {"type": "integer"}, "since_hours": {"type": "number", "default": 24}, "limit": {"type": "integer", "default": 50}}}},
    {"name": "ledger_diff", "description": "Unified diff of one change (by id from ledger_log) against the file's previous version.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
    {"name": "ledger_show", "description": "Full file content as of one change (by id from ledger_log).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
]


def mcp():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except Exception:
            continue
        mid, meth = m.get("id"), m.get("method")
        if mid is None:
            continue
        if meth == "initialize":
            res = {"protocolVersion": m.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "ledger", "version": "1.0"}}
        elif meth == "tools/list":
            res = {"tools": TOOLS}
        elif meth == "tools/call":
            p = m.get("params", {}); a = p.get("arguments") or {}
            try:
                n = p.get("name")
                if n == "ledger_log":
                    out = json.dumps(q_log(a.get("card"), a.get("since_hours", 24), min(int(a.get("limit", 50)), 200)), indent=1)
                elif n == "ledger_diff":
                    out = q_diff(a["id"])
                elif n == "ledger_show":
                    out = q_show(a["id"])[:20000]
                else:
                    out = f"unknown tool {n}"
                res = {"content": [{"type": "text", "text": out}]}
            except Exception as ex:
                res = {"content": [{"type": "text", "text": f"error: {ex}"}], "isError": True}
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n"); sys.stdout.flush()


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); return
    cmd = a[0]
    if cmd == "watch":
        watch()
    elif cmd == "mcp":
        mcp()
    elif cmd == "log":
        card = int(a[1]) if len(a) > 1 and a[1].isdigit() else None
        since = float(a[a.index("--since") + 1]) if "--since" in a else 24
        limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 50
        print(json.dumps(q_log(card, since, limit), indent=1))
    elif cmd == "diff":
        print(q_diff(a[1]))
    elif cmd == "show":
        print(q_show(a[1]))
    elif cmd == "restore":
        print(restore(a[1]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
