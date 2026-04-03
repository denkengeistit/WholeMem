"""WholeMem MCP Server — unified memory + workspace awareness.

Thin FastMCP wrapper over WholeMemService.  Runs as a standalone
HTTP server (streamable-http transport by default on port 8767).
MCP clients connect to http://localhost:8767/mcp.

Also exposes:
  GET  /health             — component status JSON
  POST /control/screenpipe — start/stop Screenpipe {"action": "start"|"stop"}
  POST /api/briefing       — orientation briefing (for Streamlit UI)
  POST /api/fix            — fix_this (for Streamlit UI)
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import anyio
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.fastmcp import FastMCP

from wholemem_mcp.config import WholeMemConfig, load_config
from wholemem_mcp.service import WholeMemService

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
# Module-level service — started in main(), accessed by routes and tools
# ---------------------------------------------------------------------------

_service: WholeMemService | None = None


def _svc() -> WholeMemService:
    """Get the running service (raises if not started)."""
    assert _service is not None, "WholeMemService not started"
    return _service


# ---------------------------------------------------------------------------
# MCP lifespan — per-session, just yields the service context
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _mcp_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Yields the shared service context for each MCP session."""
    yield _svc().context_dict()


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("wholemem_mcp", lifespan=_mcp_lifespan)


# ---------------------------------------------------------------------------
# Custom HTTP routes (not MCP — accessed by UI and health checks)
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    """Component health + uptime + active sessions."""
    status = await _svc().status()
    return JSONResponse(status)


@mcp.custom_route("/control/screenpipe", methods=["POST"])
async def control_screenpipe(request: Request) -> Response:
    """Start or stop the managed Screenpipe process."""
    svc = _svc()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    action = body.get("action", "")
    if action == "start":
        if svc.sp_process is not None:
            return JSONResponse({"status": "already running"})
        from wholemem_mcp.screenpipe import ScreenpipeProcess
        svc.sp_process = ScreenpipeProcess(svc.config.screenpipe)
        try:
            await svc.sp_process.start()
            return JSONResponse({"status": "started"})
        except Exception as exc:
            svc.sp_process = None
            return JSONResponse({"error": str(exc)}, status_code=500)

    elif action == "stop":
        if svc.sp_process is None:
            return JSONResponse({"status": "not running"})
        try:
            await svc.sp_process.stop()
        finally:
            svc.sp_process = None
        return JSONResponse({"status": "stopped"})

    return JSONResponse({"error": "action must be 'start' or 'stop'"}, status_code=400)


@mcp.custom_route("/api/briefing", methods=["POST"])
async def api_briefing(request: Request) -> Response:
    """Run what_are_we_doing and return the briefing text (for the UI)."""
    svc = _svc()
    try:
        body = await request.json()
    except Exception:
        body = {}

    workspace = body.get("workspace", str(Path(svc.config.watcher.path).expanduser()))
    query = body.get("query")

    parts: list[str] = []

    # Oracle briefing
    if svc.oracle:
        try:
            result = await svc.oracle.briefing(agent_name="ui", task=query, focus=query)
            parts.append(result["briefing"])
        except Exception as exc:
            logger.warning("Oracle briefing failed: %s", exc)
            parts.append("[Oracle unavailable]")

    # Open tasks
    if svc.task_store:
        try:
            tasks = svc.task_store.get_tasks()
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
    search_query = query or "recent activity"
    if svc.memory:
        try:
            memories = svc.memory.search(query=search_query, limit=5)
            if memories:
                mem_lines = ["## Relevant Memory"]
                for m in memories:
                    mem_lines.append(f"- {m.get('memory', '')}")
                parts.append("\n".join(mem_lines))
        except Exception:
            pass

    text = "\n\n".join(parts) if parts else "No information available."
    return JSONResponse({"briefing": text})


@mcp.custom_route("/api/fix", methods=["POST"])
async def api_fix(request: Request) -> Response:
    """Run fix_this and return the result (for the UI)."""
    svc = _svc()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    description = body.get("description", "")
    dry_run = body.get("dry_run", True)

    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)

    if svc.oracle is None:
        return JSONResponse({"error": "Watcher/oracle not enabled"}, status_code=503)

    try:
        result = await svc.oracle.fix(problem=description, dry_run=dry_run)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    parts = [f"Action: {result['action_taken']}"]
    if result["files_restored"]:
        parts.append(f"Files: {len(result['files_restored'])}")
        for f in result["files_restored"]:
            parts.append(f"  - {f['path']} → v{f.get('to_version', '?')}")
    parts.append(f"\n{result['explanation']}")

    return JSONResponse({"result": "\n".join(parts), "raw": result})


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
# Tools — same handlers as before, using lifespan context
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
    oracle_obj = ctx["oracle"]
    memory = ctx["memory"]
    task_store = ctx["task_store"]

    parts: list[str] = []

    if oracle_obj:
        try:
            result = await oracle_obj.briefing(
                agent_name="agent", task=params.query, focus=params.query,
            )
            parts.append(result["briefing"])
        except Exception as exc:
            logger.warning("Oracle briefing failed: %s", exc)
            parts.append("[Oracle unavailable]")

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
    version_store = ctx["version_store"]

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
    screenpipe = ctx["screenpipe"]
    summarizer = ctx["summarizer"]
    memory = ctx["memory"]
    version_store = ctx["version_store"]

    sources: list[str] = []

    try:
        items = await screenpipe.get_recent_activity(minutes=params.minutes)
        if items:
            from wholemem_mcp.summarizer import _flatten_items
            transcript = _flatten_items(items)
            sources.append(f"## Screen & Audio Activity\n{transcript}")
    except Exception as exc:
        logger.warning("Screenpipe fetch failed: %s", exc)

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
    oracle_obj = ctx["oracle"]
    task_store = ctx["task_store"]

    if oracle_obj is None:
        return "Error: Watcher/oracle not enabled. Cannot restore files."

    try:
        result = await oracle_obj.fix(problem=params.description, dry_run=params.dry_run)
    except Exception as exc:
        return f"Restoration failed: {exc}"

    parts = [f"Action: {result['action_taken']}"]
    if result["files_restored"]:
        parts.append(f"Files: {len(result['files_restored'])}")
        for f in result["files_restored"]:
            parts.append(f"  - {f['path']} → v{f.get('to_version', '?')}")
    parts.append(f"\n{result['explanation']}")

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
    memory = ctx["memory"]
    obsidian = ctx["obsidian"]
    task_store = ctx["task_store"]

    confirmations: list[str] = []

    metadata: Dict[str, Any] = {"source": "we_did_this"}
    if params.workspace:
        metadata["workspace"] = params.workspace

    try:
        result = memory.add(content=params.summary, metadata=metadata)
        ids = [r.get("id", "?") for r in result.get("results", [])]
        confirmations.append(f"Memory stored (IDs: {', '.join(ids)})")
    except Exception as exc:
        confirmations.append(f"Memory write failed: {exc}")

    if params.task_id:
        from wholemem_mcp.tasks import TaskStore
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
    memory = ctx["memory"]

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

async def _run_server() -> None:
    """Start the service, then serve MCP + REST over HTTP."""
    global _service

    cfg = load_config()
    _service = WholeMemService(cfg)
    await _service.start()

    # Configure transport from config
    transport = cfg.server.transport
    mcp.settings.host = cfg.server.host
    mcp.settings.port = cfg.server.port

    logger.info(
        "WholeMem server starting on %s:%d (transport=%s)",
        cfg.server.host, cfg.server.port, transport,
    )

    if transport == "stdio":
        # stdio fallback — no HTTP server
        try:
            await mcp.run_stdio_async()
        finally:
            await _service.stop()
            _service = None
        return

    import uvicorn

    if transport == "sse":
        app = mcp.sse_app()
    else:
        app = mcp.streamable_http_app()

    uv_config = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)

    # Install signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        if _service is not None:
            await _service.stop()
            _service = None


def main() -> None:
    """Run the WholeMem server (HTTP transport by default)."""
    anyio.run(_run_server)


if __name__ == "__main__":
    main()
