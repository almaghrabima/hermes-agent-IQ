"""turso_vector — self-improving long-term memory on Turso/libSQL native vectors.

See docs/superpowers/specs/2026-06-27-turso-vector-memory-design.md.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

from . import embedder as _embedder_mod
from .store import VectorStore

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "embedding_backend": "local", "embedding_model": "all-MiniLM-L6-v2",
    "embedding_dim": 384, "top_k": 8, "candidate_pool": 64, "ema_alpha": 0.4,
    "decay_rate": 0.98, "weight_floor": 0.15, "project_boost": 0.1, "beta": 0.2,
    "auto_extract": False,
    # Relevance gate: drop candidates whose cosine distance exceeds this threshold
    # (dist=0 is identical, dist=1 is orthogonal). 0.9 keeps results with >=10% similarity.
    "max_dist": 0.9,
}
_DB_LABEL = "memory_vec.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TursoVectorMemoryProvider(MemoryProvider):
    """Vector-backed, self-improving long-term memory."""

    def __init__(self) -> None:
        self._enabled = False
        self._settings: Dict[str, Any] = dict(_DEFAULTS)
        self._store = None
        self._embedder = None
        self._executor = None
        self._session_id = ""
        self._project = None
        self._cwd = None
        self._retrieved: Dict[int, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "turso_vector"

    def is_available(self) -> bool:
        # Local provider: always selectable. Heavy deps are lazy-installed in
        # initialize(); failure there disables the provider gracefully.
        return True

    def _load_settings(self) -> Dict[str, Any]:
        try:
            from hermes_cli.config import load_config
            cfg = (load_config() or {}).get("turso_vector") or {}
        except Exception:
            cfg = {}
        merged = dict(_DEFAULTS)
        if isinstance(cfg, dict):
            merged.update({k: v for k, v in cfg.items() if k in _DEFAULTS})
        return merged

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._cwd = kwargs.get("cwd") or os.getcwd()
        self._project = os.path.basename(self._cwd.rstrip("/")) if self._cwd else None
        try:
            from agent.db_backend import connect, resolve_sync_config
            from hermes_constants import get_hermes_home

            self._settings = self._load_settings()
            self._embedder = _embedder_mod.make_embedder(self._settings)
            db_path = get_hermes_home() / "vector" / _DB_LABEL
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(str(db_path), label=_DB_LABEL,
                           sync=resolve_sync_config(_DB_LABEL), prefer_libsql=True)
            self._store = VectorStore(conn, dim=int(self._settings["embedding_dim"]))
            self._store.migrate()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso-vec")
            self._enabled = True
        except Exception as exc:
            logger.warning("turso_vector disabled (init failed): %s", exc)
            self._enabled = False

    def _submit(self, fn, *, timeout: float):
        """Run on the background executor; never block the loop past timeout."""
        fut = self._executor.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            logger.debug("turso_vector background op failed/slow: %s", exc)
            return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._enabled or not query.strip():
            return ""

        max_dist = float(self._settings.get("max_dist", 0.9))

        def _do():
            qvec = self._embedder.embed(query)
            raw = self._store.search(
                query_embedding=qvec, project=self._project,
                candidate_pool=int(self._settings["candidate_pool"]),
                top_k=int(self._settings["top_k"]),
                beta=float(self._settings["beta"]),
                project_boost=float(self._settings["project_boost"]),
            )
            # Apply relevance gate: drop hits that are effectively orthogonal.
            hits = [h for h in raw if h.get("dist", 1.0) < max_dist]
            if hits:
                self._store.mark_used([h["id"] for h in hits], _now_iso())
            return hits

        hits = self._submit(_do, timeout=8.0) or []
        if not hits:
            return ""
        for h in hits:
            self._retrieved[h["id"]] = h
        return self._format_block(hits)

    @staticmethod
    def _format_block(hits: List[Dict[str, Any]]) -> str:
        lines = ["[Long-term memory — relevant past learnings]"]
        for h in hits:
            tag = h["kind"]
            if h["kind"] == "correction" and (h["what_failed"] or h["what_worked"]):
                detail = f" (failed: {h['what_failed']}; worked: {h['what_worked']})"
            else:
                detail = ""
            lines.append(f"- [{tag}] {h['text']}{detail}")
        return "\n".join(lines)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Populated in Task 7.
        return []


def register(ctx) -> None:
    """Register turso_vector as a memory provider plugin."""
    ctx.register_memory_provider(TursoVectorMemoryProvider())
