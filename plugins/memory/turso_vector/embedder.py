"""Pluggable embeddings for turso_vector. Local model by default; API optional."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Protocol


class Embedder(Protocol):
    dim: int
    def embed(self, text: str) -> List[float]: ...


class LocalEmbedder:
    """sentence-transformers embedder. Model loads lazily on first embed()."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", dim: int = 384) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from tools.lazy_deps import ensure
            ensure("memory.turso_vector")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> List[float]:
        model = self._ensure_model()
        vec = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]


class APIEmbedder:
    """OpenAI-compatible embeddings endpoint."""

    def __init__(self, *, model: str, dim: int, api_base: str, api_key_env: str) -> None:
        self.model = model
        self.dim = dim
        self.api_base = api_base.rstrip("/")
        self._api_key = os.environ.get(api_key_env, "")

    def embed(self, text: str) -> List[float]:
        import requests
        resp = requests.post(
            f"{self.api_base}/embeddings",
            json={"model": self.model, "input": text},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        return [float(x) for x in resp.json()["data"][0]["embedding"]]


def make_embedder(config: Dict[str, Any]) -> Embedder:
    backend = str(config.get("embedding_backend") or "local").strip().lower()
    if backend == "local":
        return LocalEmbedder(
            model_name=str(config.get("embedding_model") or "all-MiniLM-L6-v2"),
            dim=int(config.get("embedding_dim") or 384),
        )
    if backend == "api":
        return APIEmbedder(
            model=str(config.get("embedding_model") or "text-embedding-3-small"),
            dim=int(config.get("embedding_dim") or 1536),
            api_base=str(config.get("embedding_api_base") or "https://api.openai.com/v1"),
            api_key_env=str(config.get("embedding_api_key_env") or "TURSO_VECTOR_EMBED_API_KEY"),
        )
    raise ValueError(f"Unknown embedding_backend: {backend!r}")
