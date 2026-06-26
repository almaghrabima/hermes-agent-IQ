"""libSQL-backed memory store for turso_memory. Opens its DB through the
sub-project #1 db_backend shim (so it gets the sqlite3-compat adapter and, when
a SyncConfig is supplied, a synced embedded replica). Vectors are NATIVE libSQL
F32_BLOB columns; similarity is computed in-database via vector_distance_cos."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from agent.db_backend import connect

logger = logging.getLogger(__name__)

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def builtin_source_key(target: str, content: str) -> str:
    """Deterministic, cross-process-stable key for mirrored built-in entries."""
    digest = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"builtin:{target}:{digest}"


def new_ulid() -> str:
    """26-char Crockford-base32 ULID: 48-bit ms timestamp + 80-bit randomness."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    val = (ts << 80) | rnd
    out = []
    for _ in range(26):
        out.append(_B32[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _vec_lit(vec) -> str:
    """Format a vector for libSQL's vector32('[...]') constructor."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _sanitize_fts(query: str) -> str:
    toks = re.findall(r"\w+", query or "", re.UNICODE)
    return " OR ".join(f'"{t}"' for t in toks) if toks else '""'


def _schema(dim: int) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'fact',
    source        TEXT NOT NULL DEFAULT 'tool',
    source_key    TEXT,
    embedding     F32_BLOB({dim}),
    embed_model   TEXT,
    trust_score   REAL NOT NULL DEFAULT 0.5,
    recall_count  INTEGER NOT NULL DEFAULT 0,
    helpful_count   INTEGER NOT NULL DEFAULT 0,
    unhelpful_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_source_key
    ON memories(source_key) WHERE source_key IS NOT NULL;
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, content='memories', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

_COLS = ("id", "content", "kind", "source", "source_key", "embed_model",
         "trust_score", "created_at")


class TursoMemoryStore:
    def __init__(self, db_path, dim: int, sync=None):
        self.db_path = Path(db_path)
        self.dim = int(dim)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # prefer_libsql=True → local libSQL when sync is None, so native vector
        # functions (F32_BLOB / vector_distance_cos) are available even offline.
        self._conn = connect(
            str(self.db_path), label="memory.db", sync=sync, prefer_libsql=True,
            check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_schema(self.dim))

    # ---- writes ----
    def add(self, content, *, kind="fact", source="tool", source_key=None,
            embedding=None, embed_model=None) -> str:
        emb_sql = "vector32(?)" if embedding else "NULL"
        emb_params = (_vec_lit(embedding),) if embedding else ()
        with self._lock:
            if source_key is not None:
                existing = self._conn.execute(
                    "SELECT id FROM memories WHERE source_key = ?", (source_key,)
                ).fetchone()
                if existing:
                    mid = existing["id"]
                    self._conn.execute(
                        f"UPDATE memories SET content=?, embedding={emb_sql}, embed_model=?, "
                        f"updated_at=? WHERE id=?",
                        (content, *emb_params, embed_model, _now(), mid),
                    )
                    return mid
            mid = new_ulid()
            now = _now()
            self._conn.execute(
                f"INSERT INTO memories (id, content, kind, source, source_key, embedding, "
                f"embed_model, created_at, updated_at) VALUES (?,?,?,?,?,{emb_sql},?,?,?)",
                (mid, content, kind, source, source_key, *emb_params, embed_model, now, now),
            )
            return mid

    def remove(self, mem_id) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            return (cur.rowcount or 0) > 0

    def feedback(self, mem_id, helpful: bool) -> None:
        delta = 0.05 if helpful else -0.10
        col = "helpful_count" if helpful else "unhelpful_count"
        with self._lock:
            self._conn.execute(
                f"UPDATE memories SET trust_score = MAX(0.0, MIN(1.0, trust_score + ?)), "
                f"{col} = {col} + 1, updated_at = ? WHERE id = ?",
                (delta, _now(), mem_id),
            )

    def bump_recall(self, ids) -> None:
        if not ids:
            return
        with self._lock:
            qs = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE memories SET recall_count = recall_count + 1 WHERE id IN ({qs})",
                tuple(ids),
            )

    # ---- reads ----
    def get(self, mem_id) -> dict | None:
        row = self._conn.execute(
            f"SELECT {','.join(_COLS)} FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return {c: row[c] for c in _COLS} if row else None

    def fts_search(self, query, limit: int = 50) -> list[str]:
        try:
            rows = self._conn.execute(
                "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
                "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                (_sanitize_fts(query), limit),
            ).fetchall()
            return [r["id"] for r in rows]
        except Exception as exc:
            logger.debug("turso_memory fts_search failed: %s", exc)
            return []

    def vector_search(self, query_vec, limit: int = 50) -> list[str]:
        """Native in-database nearest-neighbour by cosine distance."""
        if not query_vec:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id FROM memories WHERE embedding IS NOT NULL "
                "ORDER BY vector_distance_cos(embedding, vector32(?)) ASC LIMIT ?",
                (_vec_lit(query_vec), limit),
            ).fetchall()
            return [r["id"] for r in rows]
        except Exception as exc:
            logger.debug("turso_memory vector_search failed: %s", exc)
            return []

    def rows_for(self, ids) -> dict:
        ids = list(ids)
        if not ids:
            return {}
        qs = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT {','.join(_COLS)} FROM memories WHERE id IN ({qs})", tuple(ids)
        ).fetchall()
        return {r["id"]: {c: r[c] for c in _COLS} for r in rows}

    def find_by_source_key(self, source_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM memories WHERE source_key = ?", (source_key,)
        ).fetchone()
        return row["id"] if row else None

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            sync = getattr(self._conn, "sync", None)
            if callable(sync):
                try:
                    sync()
                except Exception:
                    pass
            self._conn.close()
