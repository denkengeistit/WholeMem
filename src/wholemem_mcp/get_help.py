"""Get Help — request assistance from a frontier model via Synthetic API.

Add to server.py tool registration:

    from wholemem_mcp.get_help import register_get_help_tools
    register_get_help_tools(mcp)

Config via env:

    CONSULT_FRONTIER_API_KEY    required (Synthetic API key)

The tool calls the Synthetic API (https://api.synthetic.dev/v1) with the
Kimi K3 model (hf:moonshotai/Kimi-K3) to get diagnosis and suggested fixes
for errors or stuck states.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import httpx

# API configuration
SYNTHETIC_API_BASE_URL = "https://api.synthetic.dev/v1"
API_KEY = os.environ.get("CONSULT_FRONTIER_API_KEY", "")
MODEL = "hf:moonshotai/Kimi-K3"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def _build_prompt(goal: str, context: str, error: str) -> str:
    """Build a structured prompt for the frontier model."""
    return f"""You are an expert coding assistant helping diagnose and fix a problem.

## Goal
{goal}

## Context
{context}

## Error / Stuck State
{error}

Please provide:
1. A concise diagnosis of what's going wrong
2. A suggested fix (with code snippets if relevant)
3. Any additional context or considerations

Keep your response focused and actionable."""


async def _call_synthetic_api(prompt: str) -> Dict[str, Any]:
    """Call the Synthetic API with Kimi K3 model."""
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.3,  # Lower temperature for more focused diagnosis
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{SYNTHETIC_API_BASE_URL}/chat/completions",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def register_get_help_tools(mcp) -> None:
    """Attach get_help tool to the FastMCP server."""

    @mcp.tool(
        name="get_help",
        annotations={
            "title": "Consult Frontier Model for Help",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_help(
        goal: str,
        context: str,
        error: str,
    ) -> str:
        """Request help from a frontier model (Kimi K3) when stuck or encountering errors.

        Use this tool when you need a second opinion on a problem, can't figure
        out why something isn't working, or want to verify your approach before
        proceeding.

        Args:
            goal: What you are trying to achieve.
            context: Relevant code, file paths, or situation description.
            error: The error message or description of the stuck state.

        Returns:
            A diagnosis and suggested fix from the frontier model.
        """
        if not API_KEY:
            return json.dumps(
                {"success": False, "error": "CONSULT_FRONTIER_API_KEY not set"},
                indent=2,
            )

        prompt = _build_prompt(goal, context, error)

        try:
            result = await _call_synthetic_api(prompt)

            # Extract the assistant's response
            choices = result.get("choices", [])
            if not choices:
                return json.dumps(
                    {"success": False, "error": "No response from API"},
                    indent=2,
                )

            content = choices[0].get("message", {}).get("content", "")

            # Include usage info if available
            usage = result.get("usage", {})
            usage_info = f"Tokens used: {usage.get('total_tokens', 'N/A')}" if usage else ""

            response = {
                "success": True,
                "model": MODEL,
                "diagnosis_and_fix": content,
            }
            if usage_info:
                response["usage"] = usage_info

            return json.dumps(response, indent=2)

        except httpx.HTTPStatusError as exc:
            error_detail = exc.response.text
            return json.dumps(
                {"success": False, "error": f"API error: {exc}", "details": error_detail},
                indent=2,
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": f"Request failed: {exc}"},
                indent=2,
            )
