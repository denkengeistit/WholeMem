"""AnythingLLM agent dispatch — WholeMem MCP tools.

Drop into src/wholemem_mcp/anythingllm_dispatch.py and register in
server.py after the other tools:

    from wholemem_mcp.anythingllm_dispatch import register_anythingllm_tools
    register_anythingllm_tools(mcp)

Config via env (matches the WHOLEMEM_* override convention):

    WHOLEMEM_ANYTHINGLLM_BASE_URL     default http://localhost:3001
    WHOLEMEM_ANYTHINGLLM_API_KEY      required (AnythingLLM Settings > Developer API)
    WHOLEMEM_ANYTHINGLLM_IDLE_MIN     minutes of silence = idle (default 30)

Design notes:
- @agent only triggers on the workspace/thread chat endpoints, NOT the
  OpenAI-compatible endpoint, so we use /api/v1/workspace/.../thread/...
- Dispatch is fire-and-forget: agent runs can take minutes, so the POST
  runs as a background asyncio task and the tool returns immediately
  with the thread slug. Check progress with anythingllm_status.
- "Busy" = a dispatch this process started is still in flight, or the
  workspace has persisted chat activity newer than IDLE_MIN.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("wholemem.anythingllm")

BASE_URL = os.environ.get(
    "WHOLEMEM_ANYTHINGLLM_BASE_URL", "http://localhost:3001"
).rstrip("/")
API_KEY = os.environ.get("WHOLEMEM_ANYTHINGLLM_API_KEY", "")
IDLE_MIN = int(os.environ.get("WHOLEMEM_ANYTHINGLLM_IDLE_MIN", "30"))

# thread_slug -> {"task_id": ..., "started": epoch, "done": bool, "error": ...}
_in_flight: Dict[str, Dict[str, Any]] = {}


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    r = await client.get(f"{BASE_URL}/api/v1{path}", headers=_headers())
    r.raise_for_status()
    return r.json()


async def _post(client: httpx.AsyncClient, path: str, body: Dict[str, Any]) -> Any:
    r = await client.post(
        f"{BASE_URL}/api/v1{path}", headers=_headers(), json=body
    )
    r.raise_for_status()
    return r.json()


def _last_activity_epoch(chats: list) -> Optional[float]:
    """Newest sentAt/createdAt across a workspace chat history payload."""
    newest = None
    for c in chats or []:
        ts = c.get("sentAt") or c.get("createdAt")
        if ts is None:
            continue
        if isinstance(ts, str):
            from datetime import datetime
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        if newest is None or ts > newest:
            newest = float(ts)
    return newest


async def _workspace_busy(client: httpx.AsyncClient, slug: str) -> Dict[str, Any]:
    """Idle/busy verdict for a workspace."""
    # 1. Anything we dispatched still running?
    for thread, info in _in_flight.items():
        if not info.get("done"):
            return {
                "busy": True,
                "reason": f"dispatch in flight on thread {thread}",
                "task_id": info.get("task_id"),
            }
    # 2. Recent persisted chat activity?
    data = await _get(client, f"/workspace/{slug}/chats")
    chats = data.get("history") or data.get("chats") or []
    newest = _last_activity_epoch(chats)
    if newest and (time.time() - newest) < IDLE_MIN * 60:
        return {
            "busy": True,
            "reason": f"chat activity {int((time.time() - newest) / 60)} min ago",
        }
    return {"busy": False, "reason": "idle"}


async def _run_dispatch(slug: str, thread_slug: str, message: str) -> None:
    """Background task: post @agent message, record outcome."""
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            result = await _post(
                client,
                f"/workspace/{slug}/thread/{thread_slug}/chat",
                {"message": message, "mode": "chat"},
            )
        _in_flight[thread_slug]["done"] = True
        _in_flight[thread_slug]["response_preview"] = str(
            result.get("textResponse", "")
        )[:500]
        logger.info("Dispatch complete on thread %s", thread_slug)
    except Exception as exc:
        _in_flight[thread_slug]["done"] = True
        _in_flight[thread_slug]["error"] = str(exc)
        logger.warning("Dispatch failed on thread %s: %s", thread_slug, exc)


def register_anythingllm_tools(mcp) -> None:
    """Attach AnythingLLM tools to the FastMCP server."""

    @mcp.tool(
        name="anythingllm_status",
        annotations={
            "title": "AnythingLLM Agent Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def anythingllm_status(workspace_slug: str) -> str:
        """Check whether an AnythingLLM workspace has an active agent session.

        Returns JSON: reachable, busy/idle verdict, and any dispatches
        started by this server (with completion state and errors).

        Args:
            workspace_slug: AnythingLLM workspace slug.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                verdict = await _workspace_busy(client, workspace_slug)
        except Exception as exc:
            return json.dumps({"reachable": False, "error": str(exc)})
        return json.dumps(
            {"reachable": True, **verdict, "dispatches": _in_flight}, indent=2
        )

    @mcp.tool(
        name="dispatch_agent",
        annotations={
            "title": "Dispatch Task to AnythingLLM Agent",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def dispatch_agent(
        workspace_slug: str,
        task_text: str,
        task_id: Optional[str] = None,
        force: bool = False,
    ) -> str:
        """Send a task to an AnythingLLM workspace agent if it is idle.

        Creates a new thread (named after task_id when given), posts the
        task prefixed with @agent, and returns immediately — the agent
        runs in the background. Poll anythingllm_status for the outcome.
        Refuses to dispatch when the workspace looks busy unless force=true.

        Args:
            workspace_slug: AnythingLLM workspace slug.
            task_text: The task instruction for the agent.
            task_id: Optional TASKS.md 🆔 — used as thread name and for
                later we_did_this completion.
            force: Dispatch even if the workspace looks busy.
        """
        if not API_KEY:
            return json.dumps(
                {"dispatched": False, "error": "WHOLEMEM_ANYTHINGLLM_API_KEY not set"}
            )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if not force:
                    verdict = await _workspace_busy(client, workspace_slug)
                    if verdict["busy"]:
                        return json.dumps({"dispatched": False, **verdict})
                name = f"task-{task_id}" if task_id else f"task-{int(time.time())}"
                name = re.sub(r"[^a-zA-Z0-9-]", "-", name)
                thread = await _post(
                    client,
                    f"/workspace/{workspace_slug}/thread/new",
                    {"name": name},
                )
                thread_slug = (thread.get("thread") or {}).get("slug") or name
        except Exception as exc:
            return json.dumps({"dispatched": False, "error": str(exc)})

        _in_flight[thread_slug] = {
            "task_id": task_id,
            "started": time.time(),
            "done": False,
        }
        asyncio.get_running_loop().create_task(
            _run_dispatch(workspace_slug, thread_slug, f"@agent {task_text}")
        )
        return json.dumps(
            {"dispatched": True, "thread": thread_slug, "task_id": task_id}
        )
