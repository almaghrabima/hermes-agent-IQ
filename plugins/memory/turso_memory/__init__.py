"""Turso memory provider — device-synced libSQL store with hybrid semantic recall."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .store import TursoMemoryStore, builtin_source_key
from .encoder import get_encoder, EncoderUnavailable
from .retrieval import fuse_and_rank

logger = logging.getLogger(__name__)

MEMORY_TOOL_SCHEMA = {
    "type": "function",
    "name": "memory",
    "description": (
        "Durable long-term memory with semantic recall. "
        "action='remember' stores a fact; 'recall' searches by meaning; "
        "'forget' removes by id or query; 'feedback' rates a recalled memory "
        "(helpful/unhelpful) to train ranking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["remember", "recall", "forget", "feedback"]},
            "content": {"type": "string", "description": "fact text (remember)"},
            "query": {"type": "string", "description": "search text (recall/forget-by-query)"},
            "id": {"type": "string", "description": "memory id (forget/feedback)"},
            "k": {"type": "integer", "description": "max results (recall)", "default": 5},
            "helpful": {"type": "boolean", "description": "rating (feedback)"},
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

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "memory" or not self._store:
            return tool_error(f"Unknown tool: {tool_name}")
        action = args.get("action")
        try:
            if action == "remember":
                content = args["content"]
                vec, model = self._embed(content)
                mid = self._store.add(content, kind="fact", source="tool",
                                      embedding=vec, embed_model=model)
                return json.dumps({"id": mid, "status": "stored"})
            if action == "recall":
                results = self._recall(args.get("query", ""), int(args.get("k", 5)))
                return json.dumps({"results": results})
            if action == "forget":
                if args.get("id"):
                    return json.dumps({"removed": self._store.remove(args["id"])})
                hits = self._recall(args.get("query", ""), 1)
                if hits:
                    return json.dumps({"removed": self._store.remove(hits[0]["id"]), "id": hits[0]["id"]})
                return json.dumps({"removed": False})
            if action == "feedback":
                self._store.feedback(args["id"], bool(args.get("helpful", True)))
                return json.dumps({"status": "ok"})
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
        vec_ids = self._store.vector_search(vec, limit=max(20, k * 4)) if vec else []
        rows = self._store.rows_for(set(fts_ids) | set(vec_ids))
        ranked = fuse_and_rank(fts_ids, vec_ids, rows, k=k)
        self._store.bump_recall([r["id"] for r in ranked])
        return [{"id": r["id"], "content": r["content"], "score": round(r["_score"], 4)} for r in ranked]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store or not query:
            return ""
        try:
            results = self._recall(query, 5)
        except Exception:
            return ""
        if not results:
            return ""
        lines = "\n".join(f"- {r['content']}" for r in results)
        return "## Long-term memory\n" + lines

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

    def shutdown(self) -> None:
        if self._store:
            try:
                self._store.close()
            finally:
                self._store = None


def register(ctx) -> None:
    ctx.register_memory_provider(TursoMemoryProvider())
