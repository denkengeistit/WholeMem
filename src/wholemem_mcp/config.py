"""Configuration for WholeMem MCP server.

Loads settings from environment variables (WHOLEMEM_ prefix) and an optional
config.yaml in the working directory or ~/.wholemem/config.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ScreenpipeConfig(BaseModel):
    url: str = Field(default="http://localhost:3030", description="Screenpipe REST API base URL")
    managed: bool = Field(default=True, description="Start/stop Screenpipe process with the MCP server")
    command: str = Field(default="npx screenpipe@latest", description="Command to launch Screenpipe")
    api_key: str = Field(default="", description="Screenpipe local API bearer token")
    disable_telemetry: bool = Field(default=True, description="Pass --disable-telemetry to Screenpipe")
    extra_args: List[str] = Field(
        default=["--disable-audio", "--use-all-monitors"],
        description="Additional CLI flags passed to the Screenpipe record command",
    )


class LLMConfig(BaseModel):
    """LLM for oracle, mem0 fact extraction, and heavy tasks."""
    base_url: str = Field(default="http://localhost:1234/v1", description="OpenAI-compatible endpoint")
    model: str = Field(default="qwen3-1.7b", description="Model name")
    api_key: str = Field(default="lm-studio", description="API key (most local servers accept any string)")
    temperature: float = Field(default=0.3, description="Sampling temperature")
    max_tokens: int = Field(default=1024, description="Max tokens to generate")


class SummarizerConfig(BaseModel):
    """LLM for the summarizer (daemon + what_did_we_do). Can be a smaller/cheaper model.

    Defaults to apfel (Apple Intelligence on-device model) if available,
    otherwise falls back to the main LLM config.
    """
    base_url: str = Field(default="", description="OpenAI-compatible endpoint. Empty = use llm.base_url")
    model: str = Field(default="", description="Model name. Empty = use llm.model")
    api_key: str = Field(default="", description="API key. Empty = use llm.api_key")
    temperature: float = Field(default=0.3, description="Sampling temperature")
    max_tokens: int = Field(default=512, description="Max tokens to generate (smaller for summaries)")


class EmbedderConfig(BaseModel):
    provider: str = Field(default="openai", description="'openai' (any OpenAI-compatible endpoint) or 'ollama'")
    model: str = Field(default="nomic-embed-text", description="Embedding model name")
    base_url: str = Field(default="http://localhost:1234/v1", description="Embedder server URL (OpenAI-compatible)")
    embedding_dims: int = Field(default=768, description="Embedding vector dimensions")


class Mem0Config(BaseModel):
    user_id: str = Field(default="default_user", description="Default mem0 user scope")
    qdrant_url: str = Field(default="", description="Qdrant server URL (e.g. http://localhost:6333). If empty, uses embedded mode with qdrant_path.")
    qdrant_path: str = Field(default="~/.wholemem/qdrant", description="On-disk Qdrant storage path (embedded mode only)")


class ObsidianConfig(BaseModel):
    vault_path: str = Field(default="~/Documents/Obsidian", description="Obsidian vault root")
    daily_notes_subfolder: str = Field(default="Daily Notes", description="Subfolder for daily notes")


class DaemonConfig(BaseModel):
    interval_minutes: int = Field(default=1, description="Sync interval in minutes")


class WatcherConfig(BaseModel):
    enabled: bool = Field(default=True, description="Enable filesystem watcher")
    path: str = Field(default="~/Scratch", description="Workspace root directory to watch")
    exclude: List[str] = Field(
        default=[
            "node_modules/", ".git/", "__pycache__/", "*.pyc",
            "build/", "dist/", ".DS_Store", "*.swp", "*~", "*.tmp", ".#*",
        ],
        description="Glob patterns to exclude from watching",
    )


class VersioningConfig(BaseModel):
    compression_level: int = Field(default=3, description="Zstandard compression level")
    db_path: str = Field(default="~/.wholemem/", description="Directory for per-workspace SQLite databases")
    history_depth: int = Field(default=3, description="Max versions kept per file")


class OracleConfig(BaseModel):
    history_depth: int = Field(default=50, description="Max version entries in oracle context")
    session_timeout_minutes: int = Field(default=30, description="Idle timeout before session is completed")


class ServerConfig(BaseModel):
    host: str = Field(default="127.0.0.1", description="Bind address for the HTTP server")
    port: int = Field(default=8767, description="Port for MCP + REST endpoints")
    transport: str = Field(default="streamable-http", description="MCP transport: 'streamable-http', 'sse', or 'stdio'")


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class WholeMemConfig(BaseModel):
    screenpipe: ScreenpipeConfig = Field(default_factory=ScreenpipeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    summarizer_llm: SummarizerConfig = Field(default_factory=SummarizerConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    mem0: Mem0Config = Field(default_factory=Mem0Config)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    versioning: VersioningConfig = Field(default_factory=VersioningConfig)
    oracle: OracleConfig = Field(default_factory=OracleConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _find_config_file() -> Optional[Path]:
    """Search for config.yaml: CWD, then parent dirs up to root, then ~/.wholemem/."""
    # Walk up from CWD (catches project root regardless of which subdir you're in)
    d = Path.cwd()
    while True:
        candidate = d / "config.yaml"
        if candidate.is_file():
            return candidate
        parent = d.parent
        if parent == d:
            break  # filesystem root
        d = parent

    # Fall back to ~/.wholemem/
    home_cfg = Path.home() / ".wholemem" / "config.yaml"
    if home_cfg.is_file():
        return home_cfg

    return None


def _env_overrides(cfg: WholeMemConfig) -> WholeMemConfig:
    """Apply WHOLEMEM_* environment variable overrides."""
    env_map = {
        "WHOLEMEM_SCREENPIPE_URL": ("screenpipe", "url"),
        "WHOLEMEM_SCREENPIPE_MANAGED": ("screenpipe", "managed"),
        "WHOLEMEM_SCREENPIPE_COMMAND": ("screenpipe", "command"),
        "WHOLEMEM_SCREENPIPE_API_KEY": ("screenpipe", "api_key"),
        "WHOLEMEM_SCREENPIPE_DISABLE_TELEMETRY": ("screenpipe", "disable_telemetry"),
        "WHOLEMEM_SCREENPIPE_EXTRA_ARGS": ("screenpipe", "extra_args"),
        "WHOLEMEM_LLM_BASE_URL": ("llm", "base_url"),
        "WHOLEMEM_LLM_MODEL": ("llm", "model"),
        "WHOLEMEM_LLM_API_KEY": ("llm", "api_key"),
        "WHOLEMEM_SUMMARIZER_BASE_URL": ("summarizer_llm", "base_url"),
        "WHOLEMEM_SUMMARIZER_MODEL": ("summarizer_llm", "model"),
        "WHOLEMEM_SUMMARIZER_API_KEY": ("summarizer_llm", "api_key"),
        "WHOLEMEM_EMBEDDER_PROVIDER": ("embedder", "provider"),
        "WHOLEMEM_EMBEDDER_MODEL": ("embedder", "model"),
        "WHOLEMEM_EMBEDDER_BASE_URL": ("embedder", "base_url"),
        "WHOLEMEM_EMBEDDER_DIMS": ("embedder", "embedding_dims"),
        "WHOLEMEM_MEM0_USER_ID": ("mem0", "user_id"),
        "WHOLEMEM_MEM0_QDRANT_URL": ("mem0", "qdrant_url"),
        "WHOLEMEM_MEM0_QDRANT_PATH": ("mem0", "qdrant_path"),
        "WHOLEMEM_OBSIDIAN_VAULT_PATH": ("obsidian", "vault_path"),
        "WHOLEMEM_OBSIDIAN_DAILY_SUBFOLDER": ("obsidian", "daily_notes_subfolder"),
        "WHOLEMEM_DAEMON_INTERVAL": ("daemon", "interval_minutes"),
        "WHOLEMEM_WATCHER_ENABLED": ("watcher", "enabled"),
        "WHOLEMEM_WATCHER_PATH": ("watcher", "path"),
        "WHOLEMEM_VERSIONING_COMPRESSION": ("versioning", "compression_level"),
        "WHOLEMEM_VERSIONING_DB_PATH": ("versioning", "db_path"),
        "WHOLEMEM_VERSIONING_HISTORY_DEPTH": ("versioning", "history_depth"),
        "WHOLEMEM_ORACLE_HISTORY_DEPTH": ("oracle", "history_depth"),
        "WHOLEMEM_ORACLE_SESSION_TIMEOUT": ("oracle", "session_timeout_minutes"),
        "WHOLEMEM_SERVER_HOST": ("server", "host"),
        "WHOLEMEM_SERVER_PORT": ("server", "port"),
        "WHOLEMEM_SERVER_TRANSPORT": ("server", "transport"),
    }

    data = cfg.model_dump()
    for env_key, (section, field) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            # Coerce typed fields
            target_type = type(data[section][field])
            if target_type is bool:
                val = val.lower() in ("true", "1", "yes")
            elif target_type is int:
                val = int(val)
            elif target_type is float:
                val = float(val)
            data[section][field] = val

    return WholeMemConfig(**data)


def load_config() -> WholeMemConfig:
    """Load configuration from YAML file (if present) + env overrides."""
    yaml_path = _find_config_file()

    if yaml_path is not None:
        with open(yaml_path, "r") as f:
            raw = yaml.safe_load(f) or {}
        cfg = WholeMemConfig(**raw)
    else:
        cfg = WholeMemConfig()

    cfg = _env_overrides(cfg)
    return cfg
