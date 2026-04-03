"""mem0 integration layer — local-only memory storage.

Configures mem0 to use any OpenAI-compatible backend for both the LLM
(fact extraction) and the embedder, with an on-disk Qdrant store.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Disable mem0 telemetry before it is imported
os.environ.setdefault("MEM0_TELEMETRY", "false")

from wholemem_mcp.config import WholeMemConfig


class MemoryStore:
    """Wrapper around mem0 Memory with local-only defaults."""

    def __init__(self, config: WholeMemConfig) -> None:
        self._cfg = config
        self._user_id = config.mem0.user_id
        self._memory = self._init_memory()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_memory(self) -> Any:
        """Build a mem0 Memory instance from the WholeMem config."""
        # Build the mem0 config dict ----------------------------------
        qdrant_config: Dict[str, Any] = {
            "collection_name": "wholemem",
            "embedding_model_dims": self._cfg.embedder.embedding_dims,
        }

        if self._cfg.mem0.qdrant_url:
            # Client/server mode — supports concurrent access
            qdrant_config["url"] = self._cfg.mem0.qdrant_url
        else:
            # Embedded mode — single process only (file lock)
            qdrant_path = str(Path(self._cfg.mem0.qdrant_path).expanduser())
            os.makedirs(qdrant_path, exist_ok=True)
            qdrant_config["path"] = qdrant_path

        mem0_config: Dict[str, Any] = {
            "vector_store": {
                "provider": "qdrant",
                "config": qdrant_config,
            },
            "version": "v1.1",
        }

        # LLM configuration — uses the native lmstudio provider by default
        # which handles response_format compatibility correctly.
        # Ollama users can set llm.provider to "ollama" in config.
        mem0_config["llm"] = {
            "provider": "lmstudio",
            "config": {
                "model": self._cfg.llm.model,
                "temperature": 0.1,
                "max_tokens": self._cfg.llm.max_tokens,
                "lmstudio_base_url": self._cfg.llm.base_url,
                "api_key": self._cfg.llm.api_key,
                "lmstudio_response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": {"type": "object"},
                    },
                },
            },
        }

        # Embedder configuration — default uses the lmstudio provider
        # (same server as the LLM).  Ollama is still supported by
        # setting embedder.provider to "ollama" in config.
        if self._cfg.embedder.provider == "ollama":
            mem0_config["embedder"] = {
                "provider": "ollama",
                "config": {
                    "model": self._cfg.embedder.model,
                    "ollama_base_url": self._cfg.embedder.base_url,
                },
            }
        else:
            # LM Studio embedder (default)
            mem0_config["embedder"] = {
                "provider": "lmstudio",
                "config": {
                    "model": self._cfg.embedder.model,
                    "lmstudio_base_url": self._cfg.embedder.base_url,
                    "api_key": self._cfg.llm.api_key,
                },
            }

        # Lazy import — heavy dependency
        from mem0 import Memory  # type: ignore[import-untyped]

        return Memory.from_config(mem0_config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        content: str,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store a memory (fact / observation).

        Args:
            content: Text to store.  mem0 will extract facts automatically.
            user_id: Override the default user scope.
            metadata: Optional metadata dict (e.g. source, category).

        Returns:
            mem0 add result with extracted memory IDs and events.
        """
        uid = user_id or self._user_id
        kwargs: Dict[str, Any] = {"user_id": uid}
        if metadata:
            kwargs["metadata"] = metadata
        return self._memory.add(content, **kwargs)

    def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Semantic search across stored memories.

        Returns:
            List of memory dicts with id, memory text, score, etc.
        """
        uid = user_id or self._user_id
        result = self._memory.search(query, user_id=uid)
        memories = result.get("results", [])
        return memories[:limit]

    def get_all(
        self,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve all memories for a user."""
        uid = user_id or self._user_id
        result = self._memory.get_all(user_id=uid)
        return result.get("results", [])

    def delete(self, memory_id: str) -> Dict[str, Any]:
        """Delete a specific memory by ID."""
        return self._memory.delete(memory_id=memory_id)

    def close(self) -> None:
        """Close underlying Qdrant client and SQLite connection."""
        if self._memory is None:
            return

        # Close the Qdrant vector store client
        vs = getattr(self._memory, "vector_store", None)
        if vs is not None:
            client = getattr(vs, "client", None)
            if client is not None and hasattr(client, "close"):
                client.close()

        # Close the SQLite history database
        db = getattr(self._memory, "db", None)
        if db is not None:
            conn = getattr(db, "connection", None)
            if conn is not None:
                conn.close()

    def is_available(self) -> bool:
        """Check whether mem0 is initialised and usable."""
        return self._memory is not None
