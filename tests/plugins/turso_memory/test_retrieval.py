from plugins.memory.turso_memory.retrieval import rrf_fuse, final_rank


def test_rrf_rewards_presence_in_both_lists():
    # 'b' is mid-rank in both lists; 'a' tops only one. With k0=60, appearing in
    # both should beat appearing once at rank 0.
    scores = rrf_fuse([["a", "b"], ["c", "b"]], k0=60)
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]


# ---------------------------------------------------------------------------
# final_rank helpers and tests
# ---------------------------------------------------------------------------

def _final_rows(*ids):
    return {i: {"id": i, "content": i, "weight": 1.0,
                "last_used_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z", "project": None} for i in ids}


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
