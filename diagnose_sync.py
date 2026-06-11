import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

from wholemem_mcp.config import load_config
from wholemem_mcp.screenpipe import ScreenpipeClient
from wholemem_mcp.summarizer import Summarizer
from wholemem_mcp.obsidian import ObsidianWriter
from wholemem_mcp.memory import MemoryStore

# Setup logging to console for diagnosis
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("diagnose")

async def diagnose():
    print("--- Starting Diagnosis ---")
    try:
        config = load_config()
        print(f"[OK] Config loaded: {config}")
    except Exception as e:
        print(f"[FAIL] Config loading failed: {e}")
        return

    # 1. Test Obsidian Setup
    try:
        obsidian = ObsidianWriter(config.obsidian)
        print(f"[OK] ObsidianWriter initialized for path: {obsidian._daily_dir}")
    except Exception as e:
        print(f"[FAIL] ObsidianWriter initialization failed: {e}")
        return

    # 2. Test Screenpipe Connection
    try:
        screenpipe = ScreenpipeClient(config.screenpipe)
        print(f"[OK] ScreenpipeClient initialized for URL: {config.screenpipe.url}")
        
        # Try to fetch recent activity (last 30 mins)
        items = await screenpipe.get_recent_activity(minutes=30)
        print(f"[OK] Screenpipe fetch successful. Found {len(items)} items.")
        if not items:
            print("[WARN] No items found in Screenpipe for the last 30 mins. This might be why nothing is being written.")
    except Exception as e:
        print(f"[FAIL] Screenpipe fetch failed: {e}")
        return

    # 3. Test Summarizer/LLM Connection
    try:
        summarizer = Summarizer(config.summarizer_llm, config.llm)
        print(f"[OK] Summarizer initialized for model: {summarizer._model}")
        
        if items:
            # Try to summarize the first few items
            test_summary = await summarizer.summarize_activity(items[:5])
            print(f"[OK] LLM Summary successful (sample): {test_summary[:100]}...")
            
            test_note = await summarizer.summarize_for_daily_note(items[:5])
            print(f"[OK] LLM Daily Note summary successful.")
        else:
            print("[SKIP] Skipping Summarizer test because no items were fetched.")
    except Exception as e:
        print(f"[FAIL] Summarizer/LLM failed: {e}")
        return

    # 4. Test Obsidian Writing (Dry Run via print)
    try:
        if items:
            test_content = "Diagnostic test entry."
            path = obsidian.append_entry(test_content)
            print(f"[OK] Obsidian write successful! Path: {path}")
        else:
             print("[SKIP] Skipping Obsidian write test because no items were fetched.")
    except Exception as e:
        print(f"[FAIL] Obsidian write failed: {e}")
        return

    print("--- Diagnosis Complete ---")

if __name__ == "__main__":
    asyncio.run(diagnose())
