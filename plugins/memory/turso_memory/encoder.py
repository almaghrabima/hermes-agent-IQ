"""Pluggable text->vector encoders for turso_memory. Local (fastembed) by
default; an OpenAI-compatible API encoder is opt-in. Encoders are best-effort:
callers treat EncoderUnavailable / runtime failures as 'no embedding'."""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class EncoderUnavailable(RuntimeError):
    """The configured encoder cannot be constructed (bad config / missing dep)."""


@runtime_checkable
class Encoder(Protocol):
    model_id: str
    dim: int
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class LocalEncoder:
    """fastembed (ONNX) local embeddings. Lazy-installs fastembed on first use.
    The model file downloads once on first encode and is cached thereafter."""

    def __init__(self, model: str = "BAAI/bge-m3") -> None:
        self.model_id = model
        self._model = None
        # Known dims for common models; probed on first encode otherwise.
        _known = {"BAAI/bge-m3": 1024, "BAAI/bge-small-en-v1.5": 384}
        self.dim = _known.get(model, 0)

    def _ensure(self):
        if self._model is None:
            from tools.lazy_deps import ensure
            ensure("memory.turso_memory.local")
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_id)

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        vecs = [list(map(float, v)) for v in self._model.embed(list(texts))]
        if vecs and self.dim == 0:
            self.dim = len(vecs[0])
        return vecs


def _http_post(url, json, headers, timeout):  # seam for tests to monkeypatch
    import requests
    return requests.post(url, json=json, headers=headers, timeout=timeout)


class ApiEncoder:
    """OpenAI-compatible /embeddings encoder."""

    def __init__(self, base_url: str, api_key: str,
                 model: str = "text-embedding-3-small", dim: int = 1536) -> None:
        self.model_id = model
        self.dim = dim
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def encode(self, texts: list[str]) -> list[list[float]]:
        resp = _http_post(
            f"{self._base_url}/embeddings",
            json={"model": self.model_id, "input": list(texts)},
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [list(map(float, item["embedding"])) for item in data]


def get_encoder(config: dict) -> Encoder:
    mode = (config or {}).get("mode", "local")
    if mode == "local":
        return LocalEncoder(model=(config or {}).get("model", "BAAI/bge-m3"))
    if mode == "api":
        api = (config or {}).get("api", {}) or {}
        # Repo rule: secrets live in .env, not config.yaml.
        # Read the embedding API key from TURSO_MEMORY_EMBED_API_KEY first;
        # config.yaml provides only non-secret fields (base_url, model, dim).
        # Falling back to api.api_key for back-compat, but env takes precedence.
        api_key = os.environ.get("TURSO_MEMORY_EMBED_API_KEY") or api.get("api_key", "")
        if not api.get("base_url") or not api_key:
            raise EncoderUnavailable(
                "api embedding mode requires base_url in config and "
                "TURSO_MEMORY_EMBED_API_KEY in .env "
                "(or api.api_key in config for back-compat)"
            )
        return ApiEncoder(
            base_url=api["base_url"], api_key=api_key,
            model=api.get("model", "text-embedding-3-small"), dim=int(api.get("dim", 1536)),
        )
    raise EncoderUnavailable(f"unknown embedding mode: {mode!r}")
