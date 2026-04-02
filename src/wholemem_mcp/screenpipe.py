"""Async client for the Screenpipe local REST API (localhost:3030)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from wholemem_mcp.config import ScreenpipeConfig


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

    async def get_recent_activity(self, minutes: int = 15) -> List[Dict[str, Any]]:
        """Fetch all captured content from the last N minutes.

        Returns a flat list of content items (OCR + audio).
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=minutes)

        result = await self.search(
            content_type="all",
            start_time=start.isoformat(),
            end_time=now.isoformat(),
            limit=100,
        )
        return result.get("data", [])

    async def is_available(self) -> bool:
        """Check whether Screenpipe is reachable."""
        try:
            await self.health()
            return True
        except Exception:
            return False
