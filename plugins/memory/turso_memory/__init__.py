"""Turso memory provider — device-synced libSQL store with hybrid semantic recall."""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .store import TursoMemoryStore, builtin_source_key, _now
from .encoder import get_encoder, EncoderUnavailable
from .retrieval import final_rank

logger = logging.getLogger(__name__)

MEMORY_TOOL_SCHEMA = {
    "type": "function",
    "name": "memory",
    "description": (
        "Durable long-term memory with semantic recall and a learning loop. "
        "action='remember' stores a user-asserted fact; 'report' records a "
        "learning/outcome (subject to rating + time-decay); 'recall' searches by "
        "meaning; 'rate' scores a recalled memory 0-3 (3=very useful) to train "
        "ranking; 'forget' removes by id or query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["remember", "report", "recall", "rate", "forget"]},
            "content": {"type": "string", "description": "fact/learning text (remember/report)"},
            "query": {"type": "string", "description": "search text (recall/forget-by-query)"},
            "id": {"type": "string", "description": "memory id (forget/rate)"},
            "k": {"type": "integer", "description": "max results (recall)", "default": 5},
            "score": {"type": "integer", "description": "usefulness rating 0-3 (rate)"},
        },
        "required": ["action"],
    },
}


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return ((cfg.get("plugins") or {}).get("turso_memory")) or {}
    except Exception:
        return {}


class TursoMemoryProvider(MemoryProvider):
    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store: TursoMemoryStore | None = None
        self._encoder = None
        self._encoder_failed = False
        self._session_id = ""
        self._rating_alpha = float(self._config.get("rating_alpha", 0.3))
        self._project_boost = float(self._config.get("project_boost", 0.1))
        self._decay_rate = float(self._config.get("decay_rate", 0.98))
        self._project: str | None = None
        self._cwd: str | None = None
        self._recalled_ids: set[str] = set()
        # Background prefetch state (I2: recall must not block the main turn loop)
        self._prefetch_lock = threading.Lock()
        self._prefetch_result: dict[str, str] = {}   # keyed by session_id
        self._prefetch_thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return "turso_memory"

    def is_available(self) -> bool:
        return True  # libSQL via the shim is always available; embeddings are best-effort

    def initialize(self, session_id: str, **kwargs) -> None:
        from agent.db_backend import SyncConfig
        from hermes_constants import get_hermes_home
        import os

        self._session_id = session_id
        self._cwd = os.getcwd()
        self._project = os.path.basename(self._cwd.rstrip("/")) or None
        sync = None
        sync_url = (self._config.get("sync_url") or "").strip()
        token = (os.environ.get("TURSO_AUTH_TOKEN") or "").strip()
        local_path = get_hermes_home() / "replicas" / "memory.db"
        if sync_url and token:
            sync = SyncConfig(
                sync_url=sync_url, auth_token=token,
                sync_interval=int(self._config.get("sync_interval", 60)),
                local_path=local_path,
            )
        # Resolve the encoder first — the store's F32_BLOB column needs its dim.
        if self._encoder is None:           # tests may inject a fake encoder
            try:
                self._encoder = get_encoder(self._config.get("embedding", {"mode": "local"}))
            except EncoderUnavailable as exc:
                logger.warning("turso_memory: embeddings unavailable (%s); FTS-only", exc)
                self._encoder = None
        dim = (self._encoder.dim if self._encoder and self._encoder.dim
               else int((self._config.get("embedding") or {}).get("dim", 1024)))
        db_path = local_path if sync else (get_hermes_home() / "memories" / "memory.db")
        self._store = TursoMemoryStore(db_path=db_path, dim=dim, sync=sync)
        self._reconcile_builtin()

    def _embed(self, text: str):
        if self._encoder is None or self._encoder_failed:
            return None, None
        try:
            vec = self._encoder.encode([text])[0]
            return vec, self._encoder.model_id
        except Exception as exc:
            logger.debug("turso_memory embed failed: %s", exc)
            self._encoder_failed = True
            return None, None

    def _store_one(self, content: str, *, kind: str, source: str = "tool") -> str:
        vec, model = self._embed(content)
        return self._store.add(content, kind=kind, source=source,
                               project=self._project, cwd=self._cwd,
                               embedding=vec, embed_model=model)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def get_config_schema(self):
        """Config schema for `hermes memory setup`.

        Secrets (api key) go to .env; non-secret settings go to config.yaml.
        This mirrors the mem0 plugin pattern exactly.
        """
        return [
            {
                "key": "TURSO_MEMORY_EMBED_API_KEY",
                "description": "API key for OpenAI-compatible embedding endpoint (api mode only)",
                "secret": True,
                "required": False,
                "env_var": "TURSO_MEMORY_EMBED_API_KEY",
            },
            {
                "key": "sync_url",
                "description": (
                    "Turso sync URL for cross-device memory "
                    "(libsql://... from Turso dashboard). Omit for local-only."
                ),
                "secret": False,
                "required": False,
            },
            {
                "key": "sync_interval",
                "description": "Sync interval in seconds",
                "secret": False,
                "required": False,
                "default": "60",
            },
            {
                "key": "embedding.mode",
                "description": (
                    "Embedding mode: 'local' (fastembed, privacy-first, no API key) "
                    "or 'api' (OpenAI-compatible)"
                ),
                "secret": False,
                "required": False,
                "default": "local",
                "choices": ["local", "api"],
            },
            {
                "key": "embedding.model",
                "description": "Embedding model name (local: BAAI/bge-m3; api: text-embedding-3-small)",
                "secret": False,
                "required": False,
                "default": "BAAI/bge-m3",
            },
            {
                "key": "embedding.api.base_url",
                "description": "Base URL for OpenAI-compatible embedding API (api mode only)",
                "secret": False,
                "required": False,
            },
            {
                "key": "embedding.api.dim",
                "description": "Vector dimension for api mode (must match the model)",
                "secret": False,
                "required": False,
                "default": "1536",
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "memory" or not self._store:
            return tool_error(f"Unknown tool: {tool_name}")
        action = args.get("action")
        try:
            if action == "remember":
                mid = self._store_one(args["content"], kind="fact")
                return json.dumps({"id": mid, "status": "stored"})
            if action == "report":
                mid = self._store_one(args["content"], kind="learning")
                return json.dumps({"id": mid, "status": "reported"})
            if action == "recall":
                results = self._recall(args.get("query", ""), int(args.get("k", 5)))
                return json.dumps({"results": results})
            if action == "rate":
                score = int(args.get("score", -1))
                if not 0 <= score <= 3:
                    return tool_error("score must be an integer 0-3")
                self._store.rate(args["id"], score, self._rating_alpha)
                return json.dumps({"status": "ok", "id": args["id"]})
            if action == "forget":
                if args.get("id"):
                    return json.dumps({"removed": self._store.remove(args["id"])})
                hits = self._recall(args.get("query", ""), 1)
                if hits:
                    return json.dumps({"removed": self._store.remove(hits[0]["id"]), "id": hits[0]["id"]})
                return json.dumps({"removed": False})
            return tool_error(f"unknown action: {action!r}")
        except KeyError as exc:
            return tool_error(f"missing argument: {exc}")
        except Exception as exc:
            logger.debug("turso_memory tool error: %s", exc)
            return tool_error(str(exc))

    def _recall(self, query: str, k: int) -> list[dict]:
        if not query or not self._store:
            return []
        vec, _ = self._embed(query)
        fts_ids = self._store.fts_search(query, limit=max(20, k * 4))
        active_model = self._encoder.model_id if self._encoder else None
        vec_ids = self._store.vector_search(vec, limit=max(20, k * 4), embed_model=active_model) if vec else []
        rows = self._store.rows_for(set(fts_ids) | set(vec_ids))
        now = _now()
        ranked = final_rank(fts_ids, vec_ids, rows, k=k, now_iso=now,
                            active_project=self._project,
                            project_boost=self._project_boost,
                            decay_rate=self._decay_rate)
        ids = [r["id"] for r in ranked]
        self._store.mark_used(ids, now)
        self._recalled_ids.update(ids)
        return [{"id": r["id"], "content": r["content"], "score": round(r["_score"], 4)} for r in ranked]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue background recall so the next prefetch() call is fast (no I/O on hot path).

        Mirrors the mem0/supermemory/hindsight pattern: compute the recall block
        on a background thread and cache it keyed by session_id; prefetch() simply
        returns the cached string.
        """
        if not self._store or not query:
            return
        _sid = session_id or ""

        def _run() -> None:
            try:
                results = self._recall(query, 5)
            except Exception:
                return
            if not results:
                return
            lines = "\n".join(f"- {r['content']}" for r in results)
            block = "## Long-term memory\n" + lines
            with self._prefetch_lock:
                self._prefetch_result[_sid] = block

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="turso-prefetch"
        )
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return the recall block cached by the previous queue_prefetch() call.

        This must be FAST — no embedding inference or DB calls on the hot path.
        If queue_prefetch() hasn't finished yet, we wait up to 3 s (it's normally
        done well within one turn); if it's still not done, we return "" and the
        next turn will get it.
        """
        if not self._store or not query:
            return ""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            return self._prefetch_result.pop(session_id or "", "")

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
        # Capture is explicit (memory tool) + built-in mirror; turns are not auto-stored.
        return

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        if not self._store:
            return
        kind = "file_user" if target == "user" else "file_memory"
        key = builtin_source_key(target, content)
        try:
            if action in ("add", "replace"):
                vec, model = self._embed(content)
                self._store.add(content, kind=kind, source="builtin",
                                source_key=key, embedding=vec, embed_model=model)
            elif action == "remove":
                mid = self._store.find_by_source_key(key)
                if mid:
                    self._store.remove(mid)
        except Exception as exc:
            logger.debug("turso_memory mirror failed: %s", exc)

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store.count()
        except Exception:
            total = 0
        return (
            "# Long-term memory (turso_memory)\n"
            f"Active, {total} memories, semantic recall on. Use memory(action='remember') "
            "for durable facts; relevant memories are auto-recalled each turn."
        )

    def _reconcile_builtin(self) -> None:
        """Import built-in MEMORY.md / USER.md entries into the store.

        Uses the same entry delimiter as tools/memory_tool.py: ENTRY_DELIMITER = "\\n§\\n".
        Rows are keyed by builtin_source_key() — the same deterministic helper used
        in on_memory_write() — so re-running is idempotent.

        Embedding policy:
        - NEW entries are inserted with embedding=None (no encoder call).  Vectors
          fill in later via reindex() or on_memory_write() when the encoder is ready.
          This avoids forcing a fastembed model download at agent startup and prevents
          a dead/offline encoder from wiping stored vectors during reset-reconcile.
        - EXISTING entries (source_key already in the store) are skipped entirely —
          content is content-derived (key == content hash) so it cannot have changed.
        Rows that have been removed from the source files are purged.
        """
        from hermes_constants import get_hermes_home

        _ENTRY_DELIMITER = "\n§\n"   # mirrors tools/memory_tool.ENTRY_DELIMITER exactly

        base = get_hermes_home() / "memories"
        wanted: dict[str, tuple[str, str]] = {}   # source_key -> (target, content)
        for fname, target in (("MEMORY.md", "memory"), ("USER.md", "user")):
            fp = base / fname
            if not fp.exists():
                continue
            try:
                text = fp.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.split(_ENTRY_DELIMITER):
                entry = raw.strip()
                if not entry:
                    continue
                key = builtin_source_key(target, entry)
                wanted[key] = (target, entry)
        if not self._store:
            return
        # Insert only NEW entries — skip existing ones (content is key-stable).
        # Never call _embed here; vectors fill via reindex / on_memory_write.
        for key, (target, content) in wanted.items():
            if self._store.find_by_source_key(key) is not None:
                continue   # already present — no re-embed, no update
            kind = "file_user" if target == "user" else "file_memory"
            self._store.add(content, kind=kind, source="builtin",
                            source_key=key, embedding=None, embed_model=None)
        # Drop builtin rows absent from the local .md files, but ONLY when NOT syncing.
        # With sync active the DB is shared across devices, and each device has its
        # own .md files; Device B must not purge Device A's mirrored builtins that
        # are absent from B's local file — they are valid rows on the shared replica.
        if not getattr(self._store, "_sync", None):
            rows = self._store._conn.execute(
                "SELECT id, source_key FROM memories WHERE source = 'builtin'"
            ).fetchall()
            for row in rows:
                if row["source_key"] not in wanted:
                    self._store.remove(row["id"])

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id
        if reset and self._store:
            self._recalled_ids.clear()
            self._reconcile_builtin()

    def _reindex(self) -> int:
        """Re-embed any rows whose embed_model differs from the active encoder."""
        if not self._store or self._encoder is None:
            return 0
        from .store import _vec_lit, _now
        rows = self._store._conn.execute(
            "SELECT id, content FROM memories WHERE embed_model IS NOT ? OR embedding IS NULL",
            (self._encoder.model_id,),
        ).fetchall()
        n = 0
        for row in rows:
            vec, model = self._embed(row["content"])
            if vec is None:
                continue
            self._store._conn.execute(
                "UPDATE memories SET embedding=vector32(?), embed_model=?, updated_at=? WHERE id=?",
                (_vec_lit(vec), model, _now(), row["id"]),
            )
            n += 1
        return n

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        if self._store:
            try:
                self._store.close()
            finally:
                self._store = None


def register(ctx) -> None:
    ctx.register_memory_provider(TursoMemoryProvider())
