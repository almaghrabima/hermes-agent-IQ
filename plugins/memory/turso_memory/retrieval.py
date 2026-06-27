"""Pure ranking for the turso_memory provider. RRF-fuses two already-ranked id
lists (FTS5 BM25 + native vector_distance_cos). No I/O, no vector math — vector
distances are computed in libSQL, so this module only sees id lists."""
from __future__ import annotations


def rrf_fuse(ranked_lists, k0: int = 60):
    """Reciprocal Rank Fusion: id -> sum over lists of 1/(k0 + rank)."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, _id in enumerate(ranked):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k0 + rank)
    return scores


def fuse_and_rank(fts_ids, vec_ids, rows_by_id, *, k: int):
    """Fuse the FTS id list and the native-vector id list via RRF, weight each id
    by its row's trust_score, and return the top-k rows (copies with ``_score``).

    Either list may be empty (e.g. encoder unavailable -> ``vec_ids == []``); the
    other still carries the ranking.
    """
    fused = rrf_fuse([list(fts_ids), list(vec_ids)])
    ranked_ids = sorted(
        fused.keys(),
        key=lambda cid: fused[cid] * rows_by_id.get(cid, {}).get("trust_score", 0.5),
        reverse=True,
    )
    out = []
    for cid in ranked_ids[:k]:
        row = rows_by_id.get(cid)
        if row is None:
            continue
        r = dict(row)
        r["_score"] = fused[cid] * r.get("trust_score", 0.5)
        out.append(r)
    return out
