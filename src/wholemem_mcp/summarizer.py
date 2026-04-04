"""Summarizer using any OpenAI-compatible chat completions endpoint.

Designed to work with lightweight SLMs (e.g. Qwen3-1.7B) running
in LM Studio, vLLM, Ollama, or any server exposing /v1/chat/completions.
"""

from __future__ import annotations

from typing import Any, Dict, List

from openai import AsyncOpenAI

from wholemem_mcp.config import LLMConfig, SummarizerConfig


# ---------------------------------------------------------------------------
# Helpers — flatten Screenpipe items into readable text
# ---------------------------------------------------------------------------

def _flatten_items(items: List[Dict[str, Any]]) -> str:
    """Convert Screenpipe content items into a deduplicated plain-text transcript.

    Screenpipe captures OCR every few seconds, producing near-identical text
    when the user is looking at the same window.  We deduplicate consecutive
    captures from the same app whose text content hasn't meaningfully changed
    (>80% overlap), keeping only the first occurrence with a count annotation.
    Audio and UI events are never deduplicated.
    """
    lines: List[str] = []
    # Track last OCR per app for dedup
    last_ocr: Dict[str, str] = {}  # app -> last text
    last_ocr_ts: Dict[str, str] = {}  # app -> first timestamp of this run
    ocr_counts: Dict[str, int] = {}  # app -> repeat count

    def _flush_ocr(app: str) -> None:
        """Emit a pending OCR entry with its repeat count."""
        if app in last_ocr:
            count = ocr_counts.get(app, 1)
            ts = last_ocr_ts[app]
            text = last_ocr[app]
            suffix = f" [x{count}]" if count > 1 else ""
            lines.append(f"[{ts}] (screen/{app}) {text[:500]}{suffix}")
            del last_ocr[app]
            del last_ocr_ts[app]
            ocr_counts.pop(app, None)

    def _text_similar(a: str, b: str) -> bool:
        """Check if two texts are >80% similar (by shared prefix length)."""
        if not a or not b:
            return False
        shorter = min(len(a), len(b))
        common = 0
        for i in range(shorter):
            if a[i] == b[i]:
                common += 1
            else:
                break
        return common / shorter > 0.8 if shorter > 0 else False

    for item in items:
        ctype = item.get("type", "Unknown")
        content = item.get("content", {})

        if ctype == "OCR":
            ts = content.get("timestamp", "")
            app = content.get("app_name", "unknown")
            text = content.get("text", "").strip()
            if not text:
                continue
            # Deduplicate consecutive similar OCR from same app
            if app in last_ocr and _text_similar(last_ocr[app], text):
                ocr_counts[app] = ocr_counts.get(app, 1) + 1
            else:
                _flush_ocr(app)  # emit previous run
                last_ocr[app] = text
                last_ocr_ts[app] = ts
                ocr_counts[app] = 1

        elif ctype == "Audio":
            ts = content.get("timestamp", "")
            text = content.get("transcription", "").strip()
            device = content.get("device_name", "mic")
            if text:
                lines.append(f"[{ts}] (audio/{device}) {text[:500]}")

        elif ctype == "UI":
            ts = content.get("timestamp", "")
            app = content.get("app_name", "unknown")
            text = content.get("text", "").strip()
            if text:
                lines.append(f"[{ts}] (ui/{app}) {text[:300]}")

    # Flush remaining OCR entries
    for app in list(last_ocr.keys()):
        _flush_ocr(app)

    return "\n".join(lines) if lines else "(no activity captured)"


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

class Summarizer:
    """Calls a local SLM via OpenAI-compatible API to condense activity."""

    def __init__(self, summarizer_cfg: SummarizerConfig, llm_cfg: LLMConfig) -> None:
        # Use summarizer-specific config, falling back to main LLM for empty fields
        base_url = summarizer_cfg.base_url or llm_cfg.base_url
        api_key = summarizer_cfg.api_key or llm_cfg.api_key
        self._model = summarizer_cfg.model or llm_cfg.model
        self._temperature = summarizer_cfg.temperature
        self._max_tokens = summarizer_cfg.max_tokens

        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
        )

    async def summarize_activity(self, items: List[Dict[str, Any]]) -> str:
        """Produce a concise timestamped summary of Screenpipe activity items.

        Args:
            items: List of Screenpipe content items (OCR / Audio / UI dicts).

        Returns:
            A short summary paragraph with timestamps.
        """
        transcript = _flatten_items(items)
        if transcript == "(no activity captured)":
            return "No notable activity in this period."

        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise activity summarizer. Given timestamped screen "
                        "and audio captures, produce a brief summary of what the user was "
                        "doing. Group by topic/app. Keep it under 200 words. "
                        "Preserve important timestamps. Output plain text."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
        )
        return response.choices[0].message.content or ""

    async def summarize_for_daily_note(self, items: List[Dict[str, Any]]) -> str:
        """Produce a Markdown-formatted summary suitable for an Obsidian daily note.

        Args:
            items: List of Screenpipe content items.

        Returns:
            Markdown-formatted activity summary with bullet points.
        """
        transcript = _flatten_items(items)
        if transcript == "(no activity captured)":
            return "- No notable activity in this period.\n"

        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a personal knowledge assistant. Given timestamped screen "
                        "and audio captures from a user's computer, produce a Markdown "
                        "summary for their daily note. Use bullet points grouped by "
                        "topic or application. Include timestamps in HH:MM format. "
                        "Be concise but capture key facts, decisions, and URLs. "
                        "Output only the Markdown bullet list, no heading."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
        )
        return response.choices[0].message.content or ""

    async def is_available(self) -> bool:
        """Check whether the LLM endpoint is reachable."""
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False
