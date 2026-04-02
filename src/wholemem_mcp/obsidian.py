"""Obsidian Daily Note writer.

Writes / appends timestamped activity summaries to YYYY-MM-DD.md files
inside the configured Obsidian vault's daily notes folder.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from wholemem_mcp.config import ObsidianConfig


# ---------------------------------------------------------------------------
# Frontmatter template for new daily notes
# ---------------------------------------------------------------------------

_FRONTMATTER = """\
---
date: {date}
tags: [daily-note, wholemem]
---

# {date}

"""


class ObsidianWriter:
    """Manages Obsidian Daily Note files."""

    def __init__(self, config: ObsidianConfig) -> None:
        self._vault = Path(config.vault_path).expanduser()
        self._daily_dir = self._vault / config.daily_notes_subfolder

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._daily_dir.mkdir(parents=True, exist_ok=True)

    def _note_path(self, date_str: Optional[str] = None) -> Path:
        """Return the file path for a given date (default: today UTC)."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._daily_dir / f"{date_str}.md"

    def _ensure_note(self, path: Path) -> None:
        """Create a new daily note with frontmatter if it doesn't exist."""
        if not path.exists():
            date_str = path.stem  # YYYY-MM-DD
            path.write_text(_FRONTMATTER.format(date=date_str), encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_entry(
        self,
        content: str,
        date_str: Optional[str] = None,
    ) -> str:
        """Append a timestamped entry to the daily note.

        Args:
            content: Markdown content to append (e.g. bullet-point summary).
            date_str: Override date in YYYY-MM-DD format (default: today).

        Returns:
            The path of the written file as a string.
        """
        self._ensure_dirs()
        path = self._note_path(date_str)
        self._ensure_note(path)

        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        block = f"\n## {now} — WholeMem Sync\n\n{content.rstrip()}\n"

        with open(path, "a", encoding="utf-8") as f:
            f.write(block)

        return str(path)

    def read_note(self, date_str: Optional[str] = None) -> str:
        """Read the full content of a daily note.

        Args:
            date_str: Date in YYYY-MM-DD format (default: today).

        Returns:
            The note content, or a message if no note exists.
        """
        path = self._note_path(date_str)
        if not path.exists():
            return f"No daily note found for {path.stem}."
        return path.read_text(encoding="utf-8")

    def is_available(self) -> bool:
        """Check whether the vault directory exists / is writable."""
        try:
            self._ensure_dirs()
            return self._daily_dir.is_dir()
        except Exception:
            return False
