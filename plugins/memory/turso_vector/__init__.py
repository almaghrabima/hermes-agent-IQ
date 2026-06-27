"""turso_vector — self-improving long-term memory on Turso/libSQL native vectors.

See docs/superpowers/specs/2026-06-27-turso-vector-memory-design.md.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from . import embedder as _embedder_mod
from .embedder import Embedder
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
    # API-backend endpoint config. Kept in _DEFAULTS so _load_settings (which
    # filters to known keys) actually threads them through to make_embedder
    # instead of silently dropping them back to the OpenAI default.
    "embedding_api_base": "https://api.openai.com/v1",
    "embedding_api_key_env": "TURSO_VECTOR_EMBED_API_KEY",
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
        self._store: Optional[VectorStore] = None
        self._embedder: Optional[Embedder] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._session_id = ""
        self._project: Optional[str] = None
        self._cwd: Optional[str] = None
        self._retrieved: Dict[int, Dict[str, Any]] = {}
        # Background prefetch state (I2: recall must not block the main turn loop)
        self._prefetch_lock = threading.Lock()
        self._prefetch_result: Dict[str, str] = {}   # keyed by session_id
        self._prefetch_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return "turso_vector"

    def is_available(self) -> bool:
        # Local provider: always selectable. Heavy deps are lazy-installed in
        # initialize(); failure there disables the provider gracefully.
        return True

    def _load_settings(self) -> Dict[str, Any]:
        # I1 fix: the wizard writes under config["memory"][name] (confirmed by
        # reading hermes_cli/memory_setup.py). Read from there, not top-level.
        # Also: no whitelist filter — all non-secret fields pass through so that
        # API-embedder config (embedding_api_base, embedding_model, etc.) is
        # honoured. The one true secret (embed_api_key) stays env-only.
        try:
            from hermes_cli.config import load_config
            raw = load_config() or {}
            cfg = (raw.get("memory") or {}).get("turso_vector") or {}
        except Exception:
            cfg = {}
        merged = dict(_DEFAULTS)
        if isinstance(cfg, dict):
            safe = {k: v for k, v in cfg.items() if k != "embed_api_key"}
            merged.update(safe)
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
            configured_dim = int(self._settings["embedding_dim"])

            # --- Embedding-dim validation (loud, NOT swallowed) -------------
            # A dim mismatch is a configuration bug that silently produces wrong
            # vectors or insert failures. Surface it at error level and disable
            # rather than letting it slip through as a debug log.
            emb_dim = int(getattr(self._embedder, "dim", configured_dim))
            if emb_dim != configured_dim:
                logger.error(
                    "turso_vector disabled: configured embedding_dim=%d does not "
                    "match the embedder's output dim=%d (backend=%s, model=%s). "
                    "Set embedding_dim to %d in config.yaml under 'memory.turso_vector'.",
                    configured_dim, emb_dim,
                    self._settings.get("embedding_backend"),
                    self._settings.get("embedding_model"), emb_dim)
                self._enabled = False
                return

            # get_hermes_home() resolves the profile-aware path; the hermes_home
            # kwarg is intentionally not used for the DB path so multi-instance
            # profiles route through the single canonical resolver.
            db_path = get_hermes_home() / "vector" / _DB_LABEL
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(str(db_path), label=_DB_LABEL,
                           sync=resolve_sync_config(_DB_LABEL), prefer_libsql=True)
            self._store = VectorStore(conn, dim=configured_dim)

            # Detect an existing DB created with a different dim BEFORE migrate()
            # (CREATE TABLE IF NOT EXISTS no-ops, so the table would keep its old
            # dim while we'd write configured-dim vectors into it).
            existing_dim = self._store.existing_dim()
            if existing_dim is not None and existing_dim != configured_dim:
                logger.error(
                    "turso_vector disabled: existing memory DB at %s was created "
                    "with embedding dim=%d but config requests embedding_dim=%d. "
                    "The dimension is fixed at DB creation — revert embedding_dim "
                    "to %d or migrate/recreate the DB.",
                    db_path, existing_dim, configured_dim, existing_dim)
                self._enabled = False
                return

            self._store.migrate()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso-vec")
            # Warm the (possibly heavy) local model off-thread so the first real
            # store/recall isn't a cold start. Fire-and-forget; failures are fine.
            self._executor.submit(self._warm_embedder)
            self._enabled = True
        except Exception as exc:
            logger.warning("turso_vector disabled (init failed): %s", exc)
            self._enabled = False

    def _warm_embedder(self) -> None:
        try:
            if self._embedder is not None:
                self._embedder.embed("warmup")
        except Exception as exc:  # pragma: no cover - best-effort warmup
            logger.debug("turso_vector embedder warmup skipped: %s", exc)

    def _submit(self, fn, *, timeout: float):
        """Run on the background executor; never block the loop past timeout."""
        assert self._executor is not None  # only called on the _enabled path
        fut = self._executor.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            logger.debug("turso_vector background op failed/slow: %s", exc)
            return None

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue background recall so the next prefetch() call is fast (no I/O on hot path).

        Mirrors the turso_memory pattern: compute the recall block on a background
        thread and cache it keyed by session_id; prefetch() simply returns the
        cached string. Must not block the main turn loop.
        """
        if not self._enabled or not query.strip():
            return
        _sid = session_id or ""
        max_dist = float(self._settings.get("max_dist", 0.9))

        def _run() -> None:
            try:
                assert self._embedder is not None and self._store is not None
                qvec = self._embedder.embed(query)
                raw = self._store.search(
                    query_embedding=qvec, project=self._project,
                    candidate_pool=int(self._settings["candidate_pool"]),
                    top_k=int(self._settings["top_k"]),
                    beta=float(self._settings["beta"]),
                    project_boost=float(self._settings["project_boost"]),
                )
                hits = [h for h in raw if h.get("dist", 1.0) < max_dist]
                if hits:
                    self._store.mark_used([h["id"] for h in hits], _now_iso())
                    for h in hits:
                        h["prior_last_used"] = h.get("last_used_at") or h.get("created_at")
                    with self._prefetch_lock:
                        for h in hits:
                            self._retrieved[h["id"]] = h
                block = self._format_block(hits) if hits else ""
                with self._prefetch_lock:
                    self._prefetch_result[_sid] = block
            except Exception as exc:
                logger.debug("turso_vector queue_prefetch failed: %s", exc)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="turso-vec-prefetch"
        )
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return the recall block cached by the previous queue_prefetch() call.

        Must be FAST — no embedding inference or DB calls on the hot path.
        If queue_prefetch() hasn't finished yet, wait up to 3 s (it's normally
        done well within one turn); if it's still not done, return "" and the
        next turn will get it.
        """
        if not self._enabled or not query.strip():
            return ""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            return self._prefetch_result.pop(session_id or "", "")

    @staticmethod
    def _format_block(hits: List[Dict[str, Any]]) -> str:
        lines = ["[Long-term memory — relevant past learnings]"]
        for h in hits:
            tag = h["kind"]
            if h["kind"] == "correction" and (h["what_failed"] or h["what_worked"]):
                detail = f" (failed: {h['what_failed']}; worked: {h['what_worked']})"
            else:
                detail = ""
            # Prefix each line with the memory id so the model can pass it to
            # memory_rate / memory_contradict.
            lines.append(f"- [#{h['id']}][{tag}] {h['text']}{detail}")
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
                      what_failed=None, what_worked=None) -> Optional[int]:
        def _do():
            assert self._embedder is not None and self._store is not None
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
            if mid is None:
                return {"status": "error", "error": "store op failed or timed out"}
            return {"stored_id": mid}

        if tool_name == "memory_remember":
            text = args.get("text", "")
            if not text:
                return {"error": "text is required"}
            mid = self._store_memory(kind="user", text=text)
            if mid is None:
                return {"status": "error", "error": "store op failed or timed out"}
            return {"stored_id": mid}

        if tool_name == "memory_rate":
            assert self._store is not None
            store = self._store
            alpha = float(self._settings["ema_alpha"])
            rated = []
            for r in args.get("ratings", []):
                mid = int(r.get("id", -1))
                if mid in self._retrieved:
                    # Route DB writes through the single worker thread so we never
                    # touch the libSQL connection concurrently with a timed-out
                    # future still running on the worker.
                    self._submit(
                        lambda mid=mid, score=int(r.get("score", 0)):
                        store.apply_rating(mid, score, alpha), timeout=8.0)
                    rated.append(mid)
            return {"rated": rated}

        if tool_name == "memory_contradict":
            assert self._store is not None
            store = self._store
            mid = int(args.get("id", -1))
            deleted = self._submit(lambda: store.delete(mid), timeout=8.0)
            self._retrieved.pop(mid, None)
            return {"deleted": bool(deleted)}

        return {"error": f"Unknown tool: {tool_name}"}

    def system_prompt_block(self) -> str:
        return (
            "# Long-term memory (turso_vector)\n"
            "Relevant past learnings are recalled automatically each turn, each "
            "prefixed with its id, e.g. `- [#42][correction] ...`. Pass that id "
            "to memory_rate (to score how useful a recalled memory was — improves "
            "future recall) or memory_contradict (to delete a wrong one). Use "
            "memory_report to record a new correction or insight, and "
            "memory_remember to store an explicit user fact."
        )

    def _decay_sweep(self) -> None:
        # I3 fix: decay ALL memories idle >= 1 day (not just this session's
        # retrieved rows). After queue_prefetch / mark_used, recalled rows have
        # last_used_at = now, so their idle time is ~0 and they're skipped by the
        # threshold. Non-recalled old memories now lose weight correctly.
        if not self._enabled or self._store is None:
            return
        store = self._store
        now = _now_iso()
        self._submit(lambda: store.decay_stale(
            now=now,
            decay_rate=float(self._settings["decay_rate"]),
            weight_floor=float(self._settings["weight_floor"]),
        ), timeout=8.0)
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

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret settings under ``config['memory']['turso_vector']``.

        ``_load_settings()`` reads ``config.yaml["memory"]["turso_vector"]``; this
        writes there so ``hermes memory setup`` round-trips correctly. Secrets are
        handled separately (they go to .env via the schema's ``env_var``); only the
        non-secret ``values`` arrive here.
        """
        from pathlib import Path

        import yaml

        from utils import atomic_yaml_write

        config_path = Path(hermes_home) / "config.yaml"
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            except Exception:
                existing = {}
        mem = existing.get("memory")
        if not isinstance(mem, dict):
            mem = {}
        ns = mem.get("turso_vector")
        if not isinstance(ns, dict):
            ns = {}
        ns.update(values)
        mem["turso_vector"] = ns
        existing["memory"] = mem
        atomic_yaml_write(config_path, existing)

    def shutdown(self) -> None:
        try:
            if self._prefetch_thread and self._prefetch_thread.is_alive():
                self._prefetch_thread.join(timeout=3.0)
        except Exception:
            pass
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
