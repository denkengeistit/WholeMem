"""Configuration for WholeMem MCP server.

Loads settings from environment variables (WHOLEMEM_ prefix) and an optional
config.yaml in the working directory or ~/.wholemem/config.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ScreenpipeConfig(BaseModel):
    url: str = Field(default="http://localhost:3030", description="Screenpipe REST API base URL")
    managed: bool = Field(default=True, description="Start/stop Screenpipe process with the MCP server")
    command: str = Field(default="npx screenpipe@latest", description="Command to launch Screenpipe")
    disable_telemetry: bool = Field(default=True, description="Pass --disable-telemetry to Screenpipe")


class LLMConfig(BaseModel):
    base_url: str = Field(default="http://localhost:1234/v1", description="OpenAI-compatible endpoint")
    model: str = Field(default="qwen3-1.7b", description="Model name for summarization / chat")
    api_key: str = Field(default="lm-studio", description="API key (most local servers accept any string)")
    temperature: float = Field(default=0.3, description="Sampling temperature")
    max_tokens: int = Field(default=1024, description="Max tokens to generate")


class EmbedderConfig(BaseModel):
    provider: str = Field(default="openai", description="'openai' (any OpenAI-compatible endpoint) or 'ollama'")
    model: str = Field(default="nomic-embed-text", description="Embedding model name")
    base_url: str = Field(default="http://localhost:1234/v1", description="Embedder server URL (OpenAI-compatible)")
    embedding_dims: int = Field(default=768, description="Embedding vector dimensions")


class Mem0Config(BaseModel):
    user_id: str = Field(default="default_user", description="Default mem0 user scope")
    qdrant_path: str = Field(default="~/.wholemem/qdrant", description="On-disk Qdrant storage path")


class ObsidianConfig(BaseModel):
    vault_path: str = Field(default="~/Documents/Obsidian", description="Obsidian vault root")
    daily_notes_subfolder: str = Field(default="Daily Notes", description="Subfolder for daily notes")


class DaemonConfig(BaseModel):
    interval_minutes: int = Field(default=15, description="Sync interval in minutes")


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class WholeMemConfig(BaseModel):
    screenpipe: ScreenpipeConfig = Field(default_factory=ScreenpipeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    mem0: Mem0Config = Field(default_factory=Mem0Config)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _find_config_file() -> Optional[Path]:
    """Search for config.yaml in CWD then ~/.wholemem/."""
    candidates = [
        Path.cwd() / "config.yaml",
        Path.home() / ".wholemem" / "config.yaml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _env_overrides(cfg: WholeMemConfig) -> WholeMemConfig:
    """Apply WHOLEMEM_* environment variable overrides."""
    env_map = {
        "WHOLEMEM_SCREENPIPE_URL": ("screenpipe", "url"),
        "WHOLEMEM_SCREENPIPE_MANAGED": ("screenpipe", "managed"),
        "WHOLEMEM_SCREENPIPE_COMMAND": ("screenpipe", "command"),
        "WHOLEMEM_SCREENPIPE_DISABLE_TELEMETRY": ("screenpipe", "disable_telemetry"),
        "WHOLEMEM_LLM_BASE_URL": ("llm", "base_url"),
        "WHOLEMEM_LLM_MODEL": ("llm", "model"),
        "WHOLEMEM_LLM_API_KEY": ("llm", "api_key"),
        "WHOLEMEM_EMBEDDER_PROVIDER": ("embedder", "provider"),
        "WHOLEMEM_EMBEDDER_MODEL": ("embedder", "model"),
        "WHOLEMEM_EMBEDDER_BASE_URL": ("embedder", "base_url"),
        "WHOLEMEM_EMBEDDER_DIMS": ("embedder", "embedding_dims"),
        "WHOLEMEM_MEM0_USER_ID": ("mem0", "user_id"),
        "WHOLEMEM_MEM0_QDRANT_PATH": ("mem0", "qdrant_path"),
        "WHOLEMEM_OBSIDIAN_VAULT_PATH": ("obsidian", "vault_path"),
        "WHOLEMEM_OBSIDIAN_DAILY_SUBFOLDER": ("obsidian", "daily_notes_subfolder"),
        "WHOLEMEM_DAEMON_INTERVAL": ("daemon", "interval_minutes"),
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
