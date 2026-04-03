"""WholeMem Streamlit Dashboard.

Adapted from the wawd Streamlit UI. Communicates with the WholeMem
server exclusively via HTTP — never imports or instantiates the
service directly.

First-pass scope:
  - Status (component health, active sessions)
  - Screenpipe Control (start/stop)
  - Orientation (what_are_we_doing)
  - Recovery (fix_this)

Run in a separate terminal:
    wholemem-ui
    # or: streamlit run src/wholemem_mcp/ui/app.py
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Dict

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Server connection
# ---------------------------------------------------------------------------

DEFAULT_SERVER = "http://127.0.0.1:8767"


def _server_url() -> str:
    return os.environ.get("WHOLEMEM_SERVER_URL", DEFAULT_SERVER)


def _get(path: str, timeout: float = 10.0) -> Dict[str, Any] | None:
    """GET request to the WholeMem server. Returns None on failure."""
    try:
        resp = httpx.get(f"{_server_url()}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"Server request failed: {exc}")
        return None


def _post(path: str, body: Dict[str, Any] | None = None, timeout: float = 60.0) -> Dict[str, Any] | None:
    """POST request to the WholeMem server. Returns None on failure."""
    try:
        resp = httpx.post(f"{_server_url()}{path}", json=body or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"Server request failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Formatting helpers (from wawd ui.py)
# ---------------------------------------------------------------------------


def fmt_time(ts: float) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_ago(ts: float) -> str:
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(ts)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_status():
    """System status dashboard."""
    st.header("System Status")

    health = _get("/health")
    if health is None:
        st.error("Cannot reach WholeMem server. Is it running?")
        st.code(f"Expected at: {_server_url()}")
        return

    # Uptime
    uptime = health.get("uptime_seconds", 0)
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)
    st.metric("Uptime", f"{hours}h {minutes}m {seconds}s")

    # Component health
    st.subheader("Components")
    col1, col2, col3 = st.columns(3)

    sp = health.get("screenpipe", {})
    col1.metric(
        "Screenpipe",
        "✅ Available" if sp.get("available") else "❌ Unavailable",
    )
    if sp.get("managed"):
        col1.caption("Managed mode")

    llm = health.get("llm", {})
    col2.metric(
        "LLM",
        "✅ Available" if llm.get("available") else "❌ Unavailable",
    )
    col2.caption(llm.get("model", "unknown"))

    mem0 = health.get("mem0", {})
    col3.metric(
        "mem0",
        "✅ Available" if mem0.get("available") else "❌ Unavailable",
    )

    col4, col5 = st.columns(2)
    obs = health.get("obsidian", {})
    col4.metric(
        "Obsidian",
        "✅ Available" if obs.get("available") else "❌ Unavailable",
    )

    watcher = health.get("watcher", {})
    col5.metric(
        "Watcher",
        "✅ Enabled" if watcher.get("enabled") else "⚪ Disabled",
    )
    if watcher.get("path"):
        col5.caption(watcher["path"])

    # Active sessions
    st.subheader("Active Sessions")
    sessions = health.get("sessions", {}).get("active", [])
    if sessions:
        for s in sessions:
            started = fmt_time(s["started_at"]) if s.get("started_at") else "?"
            last_seen = fmt_ago(s["last_seen_at"]) if s.get("last_seen_at") else "?"
            st.markdown(
                f"**{s.get('agent', '?')}** — {s.get('task') or 'no task'} "
                f"(since {started}, last seen {last_seen})"
            )
    else:
        st.info("No active sessions.")


def page_screenpipe():
    """Screenpipe control panel."""
    st.header("Screenpipe Control")

    health = _get("/health")
    if health is None:
        st.error("Cannot reach WholeMem server.")
        return

    sp = health.get("screenpipe", {})
    is_available = sp.get("available", False)

    if is_available:
        st.success("🔴 Screenpipe is recording")
    else:
        st.warning("⚪ Screenpipe is not running")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶️ Start Screenpipe", disabled=is_available):
            result = _post("/control/screenpipe", {"action": "start"})
            if result:
                st.success(f"Screenpipe: {result.get('status', 'unknown')}")
                st.rerun()

    with col2:
        if st.button("⏹️ Stop Screenpipe", disabled=not is_available):
            result = _post("/control/screenpipe", {"action": "stop"})
            if result:
                st.info(f"Screenpipe: {result.get('status', 'unknown')}")
                st.rerun()

    st.caption(
        "Screenpipe captures screen content and audio. "
        "Start/stop to control when your screen is being recorded."
    )


def page_orientation():
    """Orientation briefing (what_are_we_doing)."""
    st.header("Orientation Briefing")

    query = st.text_input("Topic hint (optional)", placeholder="e.g. auth, config refactor")

    if st.button("Get Briefing"):
        with st.spinner("Querying oracle..."):
            body = {}
            if query:
                body["query"] = query
            result = _post("/api/briefing", body, timeout=120.0)
            if result:
                st.markdown(result.get("briefing", "No briefing available."))


def page_recovery():
    """File recovery (fix_this)."""
    st.header("File Recovery")

    description = st.text_area(
        "Describe the problem",
        placeholder="e.g. config.yaml was broken 20 minutes ago, revert it",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔍 Preview (dry run)"):
            if not description:
                st.warning("Please describe the problem first.")
            else:
                with st.spinner("Analyzing..."):
                    result = _post("/api/fix", {
                        "description": description,
                        "dry_run": True,
                    }, timeout=120.0)
                    if result:
                        st.subheader("Recovery Plan")
                        st.text(result.get("result", "No result"))

    with col2:
        if st.button("⚡ Execute Recovery", type="primary"):
            if not description:
                st.warning("Please describe the problem first.")
            else:
                with st.spinner("Restoring files..."):
                    result = _post("/api/fix", {
                        "description": description,
                        "dry_run": False,
                    }, timeout=120.0)
                    if result:
                        if "error" in result:
                            st.error(result["error"])
                        else:
                            st.success("Recovery complete!")
                            st.text(result.get("result", ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    st.set_page_config(
        page_title="WholeMem",
        page_icon="🧠",
        layout="wide",
    )

    st.sidebar.title("🧠 WholeMem")
    st.sidebar.caption("Memory + Workspace Awareness")

    page = st.sidebar.radio(
        "Navigate",
        ["Status", "Screenpipe", "Orientation", "Recovery"],
        index=0,
    )

    if page == "Status":
        page_status()
    elif page == "Screenpipe":
        page_screenpipe()
    elif page == "Orientation":
        page_orientation()
    elif page == "Recovery":
        page_recovery()

    st.sidebar.divider()
    st.sidebar.caption(f"Server: {_server_url()}")


if __name__ == "__main__":
    main()
