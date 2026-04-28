"""macOS menu bar controller for WholeMem.

Provides a tiny status item for starting, stopping, and checking the
local WholeMem HTTP server.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from wholemem_mcp.config import load_config

try:
    import rumps
except ImportError:  # pragma: no cover - exercised only when optional dep missing
    rumps = None  # type: ignore[assignment]


SERVER_ENV_VAR = "WHOLEMEM_SERVER_URL"
REQUEST_TIMEOUT = 2.0
STREAMLIT_URL = "http://localhost:8501"
GUI_PATH = (
    f"{Path.home()}/.local/bin:"
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)


def _server_url() -> str:
    """Return the configured WholeMem server URL."""
    if SERVER_ENV_VAR in os.environ:
        return os.environ[SERVER_ENV_VAR].rstrip("/")

    cfg = load_config()
    return f"http://{cfg.server.host}:{cfg.server.port}"


class WholeMemMenuBar:
    """Small macOS menu bar app for managing the WholeMem server."""

    def __init__(self) -> None:
        if rumps is None:
            raise RuntimeError(
                "The menu bar UI requires rumps. Install it with: "
                "uv pip install -e '.[menubar]'"
            )

        self.server_url = _server_url()
        self.process: subprocess.Popen[bytes] | None = None
        self.ui_process: subprocess.Popen[bytes] | None = None
        self.app = rumps.App("WholeMem", title="WM", quit_button=None)
        self.status_item = rumps.MenuItem("Status: checking…")
        self.start_item = rumps.MenuItem("Start WholeMem", callback=self.start_server)
        self.stop_item = rumps.MenuItem("Stop WholeMem", callback=self.stop_server)
        self.start_screenpipe_item = rumps.MenuItem(
            "Start Screenpipe", callback=self.start_screenpipe
        )
        self.stop_screenpipe_item = rumps.MenuItem(
            "Stop Screenpipe", callback=self.stop_screenpipe
        )
        self.refresh_item = rumps.MenuItem("Refresh Status", callback=self.refresh_status)
        self.open_ui_item = rumps.MenuItem("Open Streamlit UI", callback=self.open_ui)
        self.quit_item = rumps.MenuItem("Quit", callback=self.quit_app)

        self.app.menu = [
            self.status_item,
            None,
            self.start_item,
            self.stop_item,
            None,
            self.start_screenpipe_item,
            self.stop_screenpipe_item,
            self.refresh_item,
            self.open_ui_item,
            None,
            self.quit_item,
        ]

        self.timer = rumps.Timer(self.refresh_status, 10)
        self.timer.start()
        self.refresh_status(None)

    def run(self) -> None:
        """Run the menu bar app."""
        self.app.run()

    def _health(self) -> dict[str, Any] | None:
        try:
            response = httpx.get(f"{self.server_url}/health", timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def _server_process_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _ui_process_running(self) -> bool:
        return self.ui_process is not None and self.ui_process.poll() is None

    def refresh_status(self, _: Any) -> None:
        """Update menu item labels based on server health."""
        health = self._health()
        if health is None:
            if self._server_process_running():
                self.app.title = "WM…"
                self.status_item.title = "Status: starting…"
                self.start_item.set_callback(None)
                self.stop_item.set_callback(self.stop_server)
                self.start_screenpipe_item.set_callback(None)
                self.stop_screenpipe_item.set_callback(None)
                return

            self.app.title = "WM!"
            self.status_item.title = "Status: stopped"
            self.start_item.set_callback(self.start_server)
            self.stop_item.set_callback(None)
            self.start_screenpipe_item.set_callback(None)
            self.stop_screenpipe_item.set_callback(None)
            return

        uptime = int(health.get("uptime_seconds", 0))
        minutes, seconds = divmod(uptime, 60)
        screenpipe = health.get("screenpipe", {})
        sp_available = screenpipe.get("available", False)
        sp_status = "on" if sp_available else "off"

        self.app.title = "WM"
        self.status_item.title = f"Status: running ({minutes}m {seconds}s, Screenpipe {sp_status})"
        self.start_item.set_callback(None)
        self.stop_item.set_callback(self.stop_server)
        self.start_screenpipe_item.set_callback(None if sp_available else self.start_screenpipe)
        self.stop_screenpipe_item.set_callback(self.stop_screenpipe if sp_available else None)

    def start_server(self, _: Any) -> None:
        """Start the WholeMem server in the background."""
        if self._health() is not None:
            self._start_ui_process(open_browser=False)
            self.refresh_status(None)
            return
        self._start_server_process()
        self._start_ui_process(open_browser=False)
        time.sleep(0.5)
        self.refresh_status(None)

    def _start_server_process(self) -> None:
        """Launch the WholeMem server process if this app has not started one."""

        if not self._server_process_running():
            env = os.environ.copy()
            env["PATH"] = f"{GUI_PATH}:{env.get('PATH', '')}"
            self.process = subprocess.Popen(
                [sys.executable, "-m", "wholemem_mcp.server"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )

    def stop_server(self, _: Any) -> None:
        """Stop the server process started by this menu bar app."""
        if not self._server_process_running():
            rumps.notification(
                "WholeMem",
                "Server not managed by menu bar",
                "Stop it from the terminal or MCP client that started it.",
            )
            self.refresh_status(None)
            return

        assert self.process is not None
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        finally:
            self.process = None
            self.refresh_status(None)

    def _control_screenpipe(self, action: str) -> None:
        """Ask the WholeMem server to start or stop Screenpipe."""
        if self._health() is None:
            self.status_item.title = "Status: starting WholeMem…"
            self._start_server_process()
            for _ in range(20):
                time.sleep(0.5)
                if self._health() is not None:
                    break
            else:
                rumps.notification(
                    "WholeMem",
                    "Server did not start",
                    "Start WholeMem first, then try Screenpipe again.",
                )
                self.refresh_status(None)
                return
        self.status_item.title = f"Screenpipe: {action}ing…"
        self.start_screenpipe_item.set_callback(None)
        self.stop_screenpipe_item.set_callback(None)

        try:
            response = httpx.post(
                f"{self.server_url}/control/screenpipe",
                json={"action": action},
                timeout=45.0,
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("status") or data.get("error") or "request completed"
            rumps.notification("WholeMem", "Screenpipe", message)
        except Exception as exc:
            rumps.notification("WholeMem", "Screenpipe control failed", str(exc))
        finally:
            self.refresh_status(None)

    def start_screenpipe(self, _: Any) -> None:
        """Start Screenpipe through the WholeMem server."""
        self._control_screenpipe("start")

    def stop_screenpipe(self, _: Any) -> None:
        """Stop Screenpipe through the WholeMem server."""
        self._control_screenpipe("stop")

    def open_ui(self, _: Any) -> None:
        """Launch the Streamlit UI in a browser."""
        self._start_ui_process(open_browser=True)

    def _start_ui_process(self, open_browser: bool) -> None:
        """Launch the Streamlit UI if this app has not started one."""
        if self._ui_process_running():
            if open_browser:
                webbrowser.open(STREAMLIT_URL)
            return
        env = os.environ.copy()
        env["PATH"] = f"{GUI_PATH}:{env.get('PATH', '')}"
        self.ui_process = subprocess.Popen(
            [sys.executable, "-m", "wholemem_mcp.ui.run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        if open_browser:
            webbrowser.open(STREAMLIT_URL)

    def quit_app(self, _: Any) -> None:
        """Quit the menu bar app, leaving externally managed servers alone."""
        if self._server_process_running():
            self.stop_server(None)
        if self._ui_process_running() and self.ui_process is not None:
            try:
                os.killpg(self.ui_process.pid, signal.SIGTERM)
            except Exception:
                pass
        rumps.quit_application()


def main() -> None:
    """Run the WholeMem macOS menu bar controller."""
    WholeMemMenuBar().run()


if __name__ == "__main__":
    main()
