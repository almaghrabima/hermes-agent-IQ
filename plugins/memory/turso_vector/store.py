"""libSQL-backed vector store for turso_vector memory. All SQL lives here."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from . import weighting

_F32_BLOB_RE = re.compile(r"F32_BLOB\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _days_between(earlier_iso: str, later_iso: str) -> float:
    try:
        a = datetime.fromisoformat(earlier_iso)
        b = datetime.fromisoformat(later_iso)
        return max(0.0, (b - a).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


_CREATE = """
CREATE TABLE IF NOT EXISTS memories(
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,
  project       TEXT,
  cwd           TEXT,
  text          TEXT NOT NULL,
  what_failed   TEXT,
  what_worked   TEXT,
  embedding     F32_BLOB({dim}) NOT NULL,
  weight        REAL NOT NULL,
  ema_rating    REAL,
  use_count     INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  source_session TEXT
)
"""
_CREATE_IDX = "CREATE INDEX IF NOT EXISTS memories_project ON memories(project)"


def vec_literal(embedding: List[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class VectorStore:
    def __init__(self, conn, *, dim: int) -> None:
        self._conn = conn
        self._dim = dim

    def migrate(self) -> None:
        self._conn.execute(_CREATE.format(dim=self._dim))
        self._conn.execute(_CREATE_IDX)
        self._conn.commit()

    def existing_dim(self) -> Optional[int]:
        """Return the embedding dim declared by an EXISTING memories table.

        Parses ``F32_BLOB(<dim>)`` out of the stored CREATE TABLE statement in
        ``sqlite_master``. Returns ``None`` if the table does not yet exist (or
        the declaration can't be parsed). Call this BEFORE ``migrate()`` so a
        dim mismatch on reopen is detectable — ``CREATE TABLE IF NOT EXISTS``
        silently no-ops, so after migrate the stored sql still reflects the
        original dim.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if row is None or not row[0]:
            return None
        m = _F32_BLOB_RE.search(str(row[0]))
        return int(m.group(1)) if m else None

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(row[0])

    def insert(self, *, kind: str, project: Optional[str], cwd: Optional[str],
               text: str, what_failed: Optional[str], what_worked: Optional[str],
               embedding: List[float], created_at: str, source_session: str,
               weight: float = 1.0) -> int:
        # 13 columns: kind, project, cwd, text, what_failed, what_worked,
        # embedding, weight, ema_rating, use_count, created_at, last_used_at,
        # source_session.  ema_rating/use_count/last_used_at are literal
        # NULL/0/NULL; the 10 bound params map to the remaining 10 ? placeholders.
        cur = self._conn.execute(
            "INSERT INTO memories(kind, project, cwd, text, what_failed, "
            "what_worked, embedding, weight, ema_rating, use_count, created_at, "
            "last_used_at, source_session) "
            "VALUES (?,?,?,?,?,?,vector32(?),?,NULL,0,?,NULL,?)",
            (kind, project, cwd, text, what_failed, what_worked,
             vec_literal(embedding), weight, created_at, source_session),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def search(self, *, query_embedding: List[float], project: Optional[str],
               candidate_pool: int, top_k: int, beta: float,
               project_boost: float) -> List[Dict[str, Any]]:
        qv = vec_literal(query_embedding)
        rows = self._conn.execute(
            "SELECT id, kind, project, text, what_failed, what_worked, weight, "
            "vector_distance_cos(embedding, vector32(?)) AS dist, "
            "last_used_at, created_at "
            "FROM memories ORDER BY dist LIMIT ?",
            (qv, candidate_pool),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            rec = {
                "id": r[0], "kind": r[1], "project": r[2], "text": r[3],
                "what_failed": r[4], "what_worked": r[5], "weight": float(r[6]),
                "dist": float(r[7]),
                # Captured BEFORE mark_used() overwrites last_used_at, so the
                # decay sweep can measure real idle time at the moment of reuse.
                "last_used_at": r[8], "created_at": r[9],
            }
            rec["score"] = weighting.retrieval_score(
                rec["dist"], rec["weight"],
                project_match=(project is not None and rec["project"] == project),
                beta=beta, project_boost=project_boost,
            )
            out.append(rec)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]

    def mark_used(self, ids: List[int], now: str) -> None:
        if not ids:
            return
        for mid in ids:
            self._conn.execute(
                "UPDATE memories SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                (now, mid),
            )
        self._conn.commit()

    def apply_rating(self, mem_id: int, score: int, alpha: float) -> None:
        row = self._conn.execute(
            "SELECT weight, ema_rating FROM memories WHERE id=?", (mem_id,)
        ).fetchone()
        if row is None:
            return
        weight, prev_ema = float(row[0]), (None if row[1] is None else float(row[1]))
        new_ema = weighting.ema_update(prev_ema, score, alpha)
        new_weight = weighting.weight_from_ema(weight, new_ema)
        self._conn.execute(
            "UPDATE memories SET weight=?, ema_rating=? WHERE id=?",
            (new_weight, new_ema, mem_id),
        )
        self._conn.commit()

    def decay_and_prune(self, *, ids: List[int], now: str, decay_rate: float,
                        weight_floor: float,
                        prior_used: Optional[Mapping[int, Optional[str]]] = None) -> int:
        # ``prior_used`` maps id -> the row's last_used_at AS IT WAS before this
        # session's mark_used() bumped it to "now". Using it makes decay reflect
        # the accumulated idle time at the moment of reuse instead of ~0 days.
        # Falls back to the row's stored last_used_at, then created_at, for rows
        # that were never reused this session (preserves prior behavior).
        prior_used = prior_used or {}
        for mid in ids:
            row = self._conn.execute(
                "SELECT weight, last_used_at, created_at FROM memories WHERE id=?",
                (mid,),
            ).fetchone()
            if row is None:
                continue
            weight = float(row[0])
            ref = prior_used.get(mid) or row[1] or row[2]
            days = _days_between(ref, now)
            new_weight = weighting.decay_weight(weight, days, decay_rate)
            self._conn.execute("UPDATE memories SET weight=? WHERE id=?", (new_weight, mid))
        # Table-wide GC: intentionally prunes EVERY sub-floor row (not just the
        # ids decayed above), so memories that fell below the floor in earlier
        # sessions are also collected here.
        cur = self._conn.execute("DELETE FROM memories WHERE weight < ?", (weight_floor,))
        self._conn.commit()
        return int(cur.rowcount)

    def decay_stale(self, *, now: str, decay_rate: float, weight_floor: float,
                   min_idle_days: float = 1.0) -> int:
        """Decay every memory whose idle time >= min_idle_days, then prune sub-floor rows.

        Unlike decay_and_prune (which targets only explicitly listed ids), this
        sweeps the ENTIRE table so non-recalled rows also lose weight over time.
        Rows whose idle time is below the threshold (e.g. just-used ones) are
        left unchanged, then a table-wide GC prunes everything below weight_floor.
        """
        rows = self._conn.execute(
            "SELECT id, weight, COALESCE(last_used_at, created_at) AS ref FROM memories"
        ).fetchall()
        for row in rows:
            days = _days_between(row[2], now)
            if days >= min_idle_days:
                new_weight = weighting.decay_weight(float(row[1]), days, decay_rate)
                self._conn.execute(
                    "UPDATE memories SET weight=? WHERE id=?", (new_weight, row[0])
                )
        cur = self._conn.execute("DELETE FROM memories WHERE weight < ?", (weight_floor,))
        self._conn.commit()
        return int(cur.rowcount)

    def delete(self, mem_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self._conn.commit()
        return int(cur.rowcount) > 0
