"""mem0 integration layer — local-only memory storage.

Configures mem0 to use Ollama or any OpenAI-compatible backend for both
the LLM (fact extraction) and the embedder, with an on-disk Qdrant store.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        qdrant_path = str(Path(self._cfg.mem0.qdrant_path).expanduser())
        os.makedirs(qdrant_path, exist_ok=True)

        # Build the mem0 config dict ----------------------------------
        mem0_config: Dict[str, Any] = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "wholemem",
                    "path": qdrant_path,
                    "embedding_model_dims": self._cfg.embedder.embedding_dims,
                },
            },
            "version": "v1.1",
        }

        # LLM configuration — supports OpenAI-compatible endpoints
        # (LM Studio, vLLM, Ollama /v1, etc.)
        mem0_config["llm"] = {
            "provider": "openai",
            "config": {
                "model": self._cfg.llm.model,
                "temperature": 0.1,
                "max_tokens": self._cfg.llm.max_tokens,
                "openai_base_url": self._cfg.llm.base_url,
                "api_key": self._cfg.llm.api_key,
            },
        }

        # Embedder configuration
        if self._cfg.embedder.provider == "ollama":
            mem0_config["embedder"] = {
                "provider": "ollama",
                "config": {
                    "model": self._cfg.embedder.model,
                    "ollama_base_url": self._cfg.embedder.base_url,
                },
            }
        else:
            # Generic OpenAI-compatible embedder
            mem0_config["embedder"] = {
                "provider": "openai",
                "config": {
                    "model": self._cfg.embedder.model,
                    "openai_base_url": self._cfg.embedder.base_url,
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

    def is_available(self) -> bool:
        """Check whether mem0 is initialised and usable."""
        return self._memory is not None
