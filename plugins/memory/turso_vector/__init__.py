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

_REPORT_SCHEMA = {
    "name": "memory_report",
    "description": "Record a durable lesson for future sessions — a correction (a mistake and its fix) or an insight (something learned). Stored in long-term memory and recalled when relevant.",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["correction", "insight"]},
        "lesson": {"type": "string", "description": "The lesson, stated so it helps a future session."},
        "what_failed": {"type": "string", "description": "Corrections only: what went wrong."},
        "what_worked": {"type": "string", "description": "Corrections only: what fixed it."},
    }, "required": ["kind", "lesson"]},
}
_REMEMBER_SCHEMA = {
    "name": "memory_remember",
    "description": "Store an explicit user-provided fact or preference in long-term memory.",
    "parameters": {"type": "object", "properties": {
        "text": {"type": "string", "description": "The fact to remember."},
    }, "required": ["text"]},
}
_RATE_SCHEMA = {
    "name": "memory_rate",
    "description": "Rate how useful the recalled memories were this task (0=irrelevant, 3=very useful). Improves future recall. Only memories recalled this session can be rated.",
    "parameters": {"type": "object", "properties": {
        "ratings": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "score": {"type": "integer", "minimum": 0, "maximum": 3}},
            "required": ["id", "score"]}},
    }, "required": ["ratings"]},
}
_CONTRADICT_SCHEMA = {
    "name": "memory_contradict",
    "description": "Delete a recalled memory that is wrong or contradicted by new evidence.",
    "parameters": {"type": "object", "properties": {
        "id": {"type": "integer", "description": "The memory id to delete."},
    }, "required": ["id"]},
}

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
        if not self._enabled:
            return []
        return [_REPORT_SCHEMA, _REMEMBER_SCHEMA, _RATE_SCHEMA, _CONTRADICT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        import json
        from tools.registry import tool_error
        if not self._enabled:
            return tool_error("turso_vector memory is not active")
        try:
            return json.dumps(self._dispatch(tool_name, args))
        except Exception as exc:
            return tool_error(str(exc))

    def _store_memory(self, *, kind: str, text: str,
                      what_failed=None, what_worked=None) -> int:
        def _do():
            vec = self._embedder.embed(text)
            return self._store.insert(
                kind=kind, project=self._project, cwd=self._cwd, text=text,
                what_failed=what_failed, what_worked=what_worked, embedding=vec,
                created_at=_now_iso(), source_session=self._session_id)
        return self._submit(_do, timeout=8.0)

    def _dispatch(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "memory_report":
            lesson = args.get("lesson", "")
            if not lesson:
                return {"error": "lesson is required"}
            mid = self._store_memory(
                kind=args.get("kind", "insight"), text=lesson,
                what_failed=args.get("what_failed"), what_worked=args.get("what_worked"))
            return {"stored_id": mid}

        if tool_name == "memory_remember":
            text = args.get("text", "")
            if not text:
                return {"error": "text is required"}
            return {"stored_id": self._store_memory(kind="user", text=text)}

        if tool_name == "memory_rate":
            alpha = float(self._settings["ema_alpha"])
            rated = []
            for r in args.get("ratings", []):
                mid = int(r.get("id", -1))
                if mid in self._retrieved:
                    self._store.apply_rating(mid, int(r.get("score", 0)), alpha)
                    rated.append(mid)
            return {"rated": rated}

        if tool_name == "memory_contradict":
            mid = int(args.get("id", -1))
            deleted = self._store.delete(mid)
            self._retrieved.pop(mid, None)
            return {"deleted": deleted}

        return {"error": f"Unknown tool: {tool_name}"}

    def system_prompt_block(self) -> str:
        return (
            "# Long-term memory (turso_vector)\n"
            "Relevant past learnings are recalled automatically each turn. Use "
            "memory_report to record a correction or insight, memory_remember to "
            "store an explicit user fact, memory_rate to score how useful the "
            "recalled memories were (improves future recall), and "
            "memory_contradict to delete a wrong memory."
        )

    def _decay_sweep(self) -> None:
        if not self._enabled or not self._retrieved:
            return
        ids = list(self._retrieved.keys())
        self._submit(lambda: self._store.decay_and_prune(
            ids=ids, now=_now_iso(),
            decay_rate=float(self._settings["decay_rate"]),
            weight_floor=float(self._settings["weight_floor"])), timeout=8.0)
        self._retrieved = {}

    def on_session_end(self, messages) -> None:
        self._decay_sweep()

    def on_pre_compress(self, messages) -> str:
        self._decay_sweep()
        return ""

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "embedding_backend", "description": "Embeddings source: 'local' (offline) or 'api'.", "default": "local", "choices": ["local", "api"]},
            {"key": "embedding_model", "description": "Embedding model id.", "default": "all-MiniLM-L6-v2"},
            {"key": "embedding_dim", "description": "Embedding dimension (must match the model; fixed at first DB creation).", "default": 384},
            {"key": "embedding_api_base", "description": "API backend only: OpenAI-compatible base URL.", "default": "https://api.openai.com/v1"},
            {"key": "embed_api_key", "description": "API backend only: embeddings API key.", "secret": True, "required": False, "env_var": "TURSO_VECTOR_EMBED_API_KEY"},
            {"key": "top_k", "description": "Memories recalled per turn.", "default": 8},
            {"key": "max_dist", "description": "Max cosine distance for a memory to be recalled (0-1; lower = stricter relevance).", "default": 0.9},
            {"key": "auto_extract", "description": "Auto-extract insights at session end (experimental).", "default": False, "choices": [True, False]},
        ]

    def shutdown(self) -> None:
        try:
            if self._executor is not None:
                self._executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass
        try:
            if self._store is not None:
                self._store._conn.close()
        except Exception:
            pass


def register(ctx) -> None:
    """Register turso_vector as a memory provider plugin."""
    ctx.register_memory_provider(TursoVectorMemoryProvider())
