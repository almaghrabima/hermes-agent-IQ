"""Pure ranking for the turso_memory provider. RRF-fuses two already-ranked id
lists (FTS5 BM25 + native vector_distance_cos). No I/O, no vector math — vector
distances are computed in libSQL, so this module only sees id lists."""
from __future__ import annotations

from .weighting import days_between, decay_weight


def rrf_fuse(ranked_lists, k0: int = 60):
    """Reciprocal Rank Fusion: id -> sum over lists of 1/(k0 + rank)."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, _id in enumerate(ranked):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k0 + rank)
    return scores



def final_rank(fts_ids, vec_ids, rows_by_id, *, k, now_iso,
               active_project=None, project_boost: float = 0.1,
               decay_rate: float = 0.98) -> list[dict]:
    """RRF-fuse the FTS + vector id lists, then weight each by learned weight,
    recency decay, and project match. Returns top-k rows (copies + ``_score``)."""
    base = rrf_fuse([list(fts_ids), list(vec_ids)])
    scored = []
    for _id, b in base.items():
        row = rows_by_id.get(_id)
        if row is None:
            continue
        last = row.get("last_used_at") or row.get("created_at") or now_iso
        w = decay_weight(float(row.get("weight", 1.0)), days_between(last, now_iso), decay_rate)
        pf = (1.0 + project_boost) if (active_project and row.get("project") == active_project) else 1.0
        r = dict(row)
        r["_score"] = b * w * pf
        scored.append(r)
    scored.sort(key=lambda r: r["_score"], reverse=True)
    return scored[:k]
