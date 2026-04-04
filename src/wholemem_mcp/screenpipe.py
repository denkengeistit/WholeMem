"""Async client for the Screenpipe local REST API and process manager."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from wholemem_mcp.config import ScreenpipeConfig

logger = logging.getLogger("wholemem.screenpipe")


# ---------------------------------------------------------------------------
# Process manager — starts / stops the Screenpipe recording process
# ---------------------------------------------------------------------------

class ScreenpipeProcess:
    """Manage the Screenpipe recording subprocess."""

    def __init__(self, config: ScreenpipeConfig) -> None:
        self._cfg = config
        self._process: Optional[asyncio.subprocess.Process] = None

    def _build_args(self) -> List[str]:
        """Build the argument list for the Screenpipe subprocess."""
        parts = shlex.split(self._cfg.command)
        args = [*parts, "record"]

        # Extract port from the configured URL
        parsed = urlparse(self._cfg.url)
        port = parsed.port or 3030
        args.extend(["--port", str(port)])

        if self._cfg.disable_telemetry:
            args.append("--disable-telemetry")

        # Extra CLI flags (e.g. --disable-audio, --use-all-monitors)
        if self._cfg.extra_args:
            args.extend(self._cfg.extra_args)

        return args

    async def start(self, timeout: float = 30.0) -> None:
        """Launch Screenpipe and wait until its /health endpoint responds."""
        args = self._build_args()
        logger.info("Starting Screenpipe: %s", " ".join(args))

        # Start in a new process group so we can kill the whole tree
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        # Poll /health until ready
        health_url = f"{self._cfg.url.rstrip('/')}/health"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            # Check the process hasn't crashed
            if self._process.returncode is not None:
                stderr = b""
                if self._process.stderr:
                    stderr = await self._process.stderr.read()
                raise RuntimeError(
                    f"Screenpipe exited with code {self._process.returncode}: "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(health_url)
                    if resp.status_code == 200:
                        logger.info("Screenpipe is ready (pid %d).", self._process.pid)
                        return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1.0)

        # Timed out — kill the process we started
        await self.stop()
        raise TimeoutError(
            f"Screenpipe did not become ready within {timeout}s"
        )

    def _find_port_pid(self) -> int | None:
        """Find the PID listening on the Screenpipe port (catches orphaned grandchildren)."""
        import subprocess
        parsed = urlparse(self._cfg.url)
        port = parsed.port or 3030
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    pid = int(line.strip())
                    if pid != os.getpid():  # don't kill ourselves
                        return pid
        except Exception:
            pass
        return None

    async def stop(self) -> None:
        """Terminate the Screenpipe subprocess gracefully.

        npx spawns screenpipe as a grandchild that can outlive npx.
        We kill by process group first, then fall back to killing
        whatever is listening on the configured port.
        """
        if self._process is None or self._process.returncode is not None:
            # npx may have exited but the screenpipe grandchild survives
            port_pid = self._find_port_pid()
            if port_pid:
                logger.info("Found orphaned Screenpipe on port (pid %d), killing.", port_pid)
                os.kill(port_pid, signal.SIGTERM)
                await asyncio.sleep(1.0)
            return

        pid = self._process.pid
        logger.info("Stopping Screenpipe (pid %d)...", pid)

        # Send SIGINT to the process group (npx + children)
        try:
            os.killpg(os.getpgid(pid), signal.SIGINT)
        except (ProcessLookupError, OSError):
            self._process.send_signal(signal.SIGINT)

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        # Escalate to SIGKILL on the process group
        logger.warning("Screenpipe did not exit in 5s, killing process group.")
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                self._process.kill()
            except ProcessLookupError:
                pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass

        # Final safety net: kill whatever is still on the port
        port_pid = self._find_port_pid()
        if port_pid:
            logger.warning("Screenpipe grandchild still alive (pid %d), killing by port.", port_pid)
            try:
                os.kill(port_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        logger.info("Screenpipe stopped.")


# ---------------------------------------------------------------------------
# REST API client
# ---------------------------------------------------------------------------

class ScreenpipeClient:
    """Thin async wrapper around Screenpipe's REST endpoints."""

    def __init__(self, config: ScreenpipeConfig) -> None:
        self.base_url = config.url.rstrip("/")
        self._timeout = 30.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def health(self) -> Dict[str, Any]:
        """Return Screenpipe server health status."""
        return await self._get("/health")

    async def search(
        self,
        query: Optional[str] = None,
        content_type: str = "all",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        app_name: Optional[str] = None,
        window_name: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        min_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Search screen & audio content captured by Screenpipe.

        Args:
            query: Free-text search string.
            content_type: 'all', 'ocr', 'audio', 'ui', or combos like 'audio+ocr'.
            start_time: ISO 8601 lower bound.
            end_time: ISO 8601 upper bound.
            app_name: Filter by application name.
            window_name: Filter by window title.
            limit: Max results (1-100).
            offset: Pagination offset.
            min_length: Minimum text length filter.

        Returns:
            Screenpipe search response dict with 'data' and 'pagination'.
        """
        params: Dict[str, Any] = {
            "content_type": content_type,
            "limit": min(limit, 100),
            "offset": offset,
        }
        if query:
            params["q"] = query
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if app_name:
            params["app_name"] = app_name
        if window_name:
            params["window_name"] = window_name
        if min_length is not None:
            params["min_length"] = min_length

        return await self._get("/search", params=params)

    async def get_recent_activity(self, minutes: int = 15, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch captured content from the last N minutes.

        Returns a flat list of content items (OCR + audio).
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes)

        result = await self.search(
            content_type="all",
            start_time=start.isoformat(),
            end_time=now.isoformat(),
            limit=limit,
        )
        return result.get("data", [])

    async def is_available(self) -> bool:
        """Check whether Screenpipe is reachable."""
        try:
            await self.health()
            return True
        except Exception:
            return False
