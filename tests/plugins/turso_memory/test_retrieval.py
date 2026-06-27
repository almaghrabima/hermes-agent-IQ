from plugins.memory.turso_memory.retrieval import rrf_fuse, fuse_and_rank, final_rank


def test_rrf_rewards_presence_in_both_lists():
    # 'b' is mid-rank in both lists; 'a' tops only one. With k0=60, appearing in
    # both should beat appearing once at rank 0.
    scores = rrf_fuse([["a", "b"], ["c", "b"]], k0=60)
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]


def _rows(*ids):
    return {i: {"id": i, "content": i, "trust_score": 0.5, "created_at": "2026-01-01"} for i in ids}


def test_fuse_and_rank_orders_by_fused_then_trust():
    # 'x' appears in both the FTS and vector lists -> ranks above 'y'/'z'.
    out = fuse_and_rank(fts_ids=["x", "y"], vec_ids=["x", "z"], rows_by_id=_rows("x", "y", "z"), k=3)
    assert out[0]["id"] == "x"
    assert "_score" in out[0]
    assert {r["id"] for r in out} == {"x", "y", "z"}


def test_fuse_and_rank_vector_only_when_no_fts():
    out = fuse_and_rank(fts_ids=[], vec_ids=["b", "a"], rows_by_id=_rows("a", "b"), k=2)
    assert [r["id"] for r in out] == ["b", "a"]


def test_fuse_and_rank_fts_only_when_no_vectors():
    out = fuse_and_rank(fts_ids=["a", "b"], vec_ids=[], rows_by_id=_rows("a", "b"), k=2)
    assert [r["id"] for r in out] == ["a", "b"]


def test_fuse_and_rank_trust_breaks_ties():
    rows = _rows("a", "b")
    rows["b"]["trust_score"] = 0.9  # same rank in lists, higher trust -> first
    out = fuse_and_rank(fts_ids=["a", "b"], vec_ids=["a", "b"], rows_by_id=rows, k=2)
    # 'a' and 'b' have equal RRF (same positions in both lists); trust lifts 'b'
    assert out[0]["id"] == "b"


# ---------------------------------------------------------------------------
# final_rank helpers and tests
# ---------------------------------------------------------------------------

def _final_rows(*ids):
    return {i: {"id": i, "content": i, "weight": 1.0,
                "last_used_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z", "project": None} for i in ids}


def test_rrf_rewards_both_lists():
    s = rrf_fuse([["a", "b"], ["c", "b"]], k0=60)
    assert s["b"] > s["a"] and s["b"] > s["c"]


def test_final_rank_orders_and_scores():
    out = final_rank(["x", "y"], ["x", "z"], _final_rows("x", "y", "z"), k=3, now_iso="2026-01-01T00:00:00Z")
    assert out[0]["id"] == "x" and "_score" in out[0]


def test_final_rank_weight_lifts():
    rows = _final_rows("a", "b")
    rows["b"]["weight"] = 5.0
    out = final_rank(["a", "b"], ["a", "b"], rows, k=2, now_iso="2026-01-01T00:00:00Z")
    assert out[0]["id"] == "b"


def test_final_rank_project_boost():
    rows = _final_rows("a", "b")
    rows["b"]["project"] = "p1"
    out = final_rank(["a", "b"], ["a", "b"], rows, k=2, now_iso="2026-01-01T00:00:00Z",
                     active_project="p1", project_boost=1.0)
    assert out[0]["id"] == "b"


def test_final_rank_vector_only():
    out = final_rank([], ["b", "a"], _final_rows("a", "b"), k=2, now_iso="2026-01-01T00:00:00Z")
    assert [r["id"] for r in out] == ["b", "a"]
