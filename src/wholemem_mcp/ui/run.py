"""Entrypoint for the WholeMem Streamlit UI.

Usage:
    wholemem-ui
    # or: streamlit run src/wholemem_mcp/ui/app.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Launch the Streamlit dashboard."""
    app_path = Path(__file__).parent / "app.py"
    sys.exit(
        subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.headless=true"],
        )
    )


if __name__ == "__main__":
    main()
