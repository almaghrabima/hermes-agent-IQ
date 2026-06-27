import pytest
pytest.importorskip("libsql")  # skip whole module when libsql is absent

from plugins.memory.turso_memory.store import TursoMemoryStore, new_ulid, builtin_source_key


def _store(tmp_path) -> TursoMemoryStore:
    return TursoMemoryStore(db_path=tmp_path / "memory.db", dim=3, sync=None)


def test_new_ulid_unique_and_sortable():
    import time
    a = new_ulid()
    time.sleep(0.002)
    b = new_ulid()
    assert a != b and len(a) == len(b) == 26
    assert a < b        # ULIDs are lexicographically time-sortable


def test_add_then_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    mid = s.add("the eagle lands", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    row = s.get(mid)
    assert row["content"] == "the eagle lands"
    assert row["embed_model"] == "fake/3"
    s.close()


def test_vector_search_finds_nearest(tmp_path):
    s = _store(tmp_path)
    near = s.add("eagles", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    s.add("zebras", embedding=[0.0, 0.0, 1.0], embed_model="fake/3")
    ids = s.vector_search([0.9, 0.1, 0.0], limit=2)
    assert ids[0] == near        # nearest by cosine distance first
    s.close()


def test_fts_search_finds_added_row(tmp_path):
    s = _store(tmp_path)
    s.add("docker deployment notes", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    s.add("a poem about eagles", embedding=[0.0, 1.0, 0.0], embed_model="fake/3")
    ids = s.fts_search("eagles")
    rows = s.rows_for(ids)
    assert any("eagles" in rows[i]["content"] for i in ids)
    s.close()


def test_fts_search_unicode_arabic(tmp_path):
    s = _store(tmp_path)
    mid = s.add("القاهرة مدينة", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    ids = s.fts_search("القاهرة")
    assert mid in ids        # Unicode-aware tokenizer recalls non-Latin content
    s.close()


def test_null_embedding_excluded_from_vector_but_fts_finds_it(tmp_path):
    s = _store(tmp_path)
    mid = s.add("keyword only zebra", embedding=None)   # no vector
    assert s.vector_search([1.0, 0.0, 0.0]) == []        # excluded from vector search
    assert mid in s.fts_search("zebra")                  # still FTS-recallable
    s.close()


def test_source_key_upsert_updates_in_place(tmp_path):
    s = _store(tmp_path)
    k = "builtin:memory:abc"
    id1 = s.add("v1", source="builtin", source_key=k, embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    id2 = s.add("v2", source="builtin", source_key=k, embedding=[0.0, 1.0, 0.0], embed_model="fake/3")
    assert id1 == id2                       # same row, upserted
    assert s.get(id1)["content"] == "v2"
    assert s.count() == 1
    s.close()


def test_remove(tmp_path):
    s = _store(tmp_path)
    mid = s.add("forget me", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    assert s.remove(mid) is True
    assert s.get(mid) is None
    s.close()


# ---------- FIX M1 — builtin_source_key strips content before hashing ----------

def test_builtin_source_key_strips_whitespace():
    """Key must be identical for content with leading/trailing whitespace stripped."""
    assert builtin_source_key("memory", "  hello  ") == builtin_source_key("memory", "hello")
    assert builtin_source_key("user", "\nfact\n") == builtin_source_key("user", "fact")


# ---------- FIX I1a — upsert with embedding=None must not wipe good embedding ----------

def test_upsert_null_embedding_preserves_existing_vector(tmp_path):
    """Re-adding by source_key with embedding=None must NOT overwrite a stored embedding."""
    s = _store(tmp_path)
    k = "builtin:memory:preserve"
    # Insert with a real embedding
    mid = s.add("original content", source="builtin", source_key=k,
                 embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    # Upsert same source_key but with embedding=None (e.g. reconcile with dead encoder)
    mid2 = s.add("updated content", source="builtin", source_key=k, embedding=None)
    assert mid == mid2           # same row
    # Embedding must still be there — vector search must still find the row
    hits = s.vector_search([1.0, 0.0, 0.0], limit=5)
    assert mid in hits, "embedding was wiped by NULL upsert"
    # But content must have been updated
    assert s.get(mid)["content"] == "updated content"
    s.close()


# ---------- FIX I3 — vector_search embed_model filter ----------

def test_vector_search_model_filter_excludes_other_models(tmp_path):
    """vector_search(embed_model=X) must return [] for rows stored under model Y."""
    s = _store(tmp_path)
    s.add("row under m3", embedding=[1.0, 0.0, 0.0], embed_model="m3")
    # query filtered to a DIFFERENT model must return nothing, not raise
    hits = s.vector_search([1.0, 0.0, 0.0], limit=5, embed_model="other_model")
    assert hits == []
    s.close()


def test_vector_search_model_filter_returns_matching_model_rows(tmp_path):
    """vector_search(embed_model=X) must return rows stored under model X."""
    s = _store(tmp_path)
    mid = s.add("row under m3", embedding=[1.0, 0.0, 0.0], embed_model="m3")
    s.add("row under other", embedding=[1.0, 0.0, 0.0], embed_model="other_model")
    hits = s.vector_search([1.0, 0.0, 0.0], limit=5, embed_model="m3")
    assert mid in hits
    assert len(hits) == 1   # only the m3 row
    s.close()


# ---------- ENH Task 2 — project scoping + ratings/EMA + decay-prune ----------

def _ws(tmp_path):
    return TursoMemoryStore(db_path=tmp_path / "m.db", dim=3, sync=None)


def test_add_with_project_and_rows_for_exposes_weight(tmp_path):
    s = _ws(tmp_path)
    mid = s.add("p note", embedding=[1.0, 0.0, 0.0], embed_model="f/3", project="proj1", cwd="/tmp")
    row = s.rows_for([mid])[mid]
    assert row["project"] == "proj1" and row["weight"] == 1.0 and row["ema"] is None
    s.close()


def test_rate_updates_weight(tmp_path):
    s = _ws(tmp_path)
    g = s.add("good", embedding=[1.0, 0.0, 0.0], embed_model="f/3")
    b = s.add("bad", embedding=[0.0, 1.0, 0.0], embed_model="f/3")
    s.rate(g, 3, 0.5)
    s.rate(b, 0, 0.5)
    assert s.rows_for([g])[g]["weight"] > s.rows_for([b])[b]["weight"]
    s.close()


def test_mark_used_sets_last_used(tmp_path):
    s = _ws(tmp_path)
    mid = s.add("x", embedding=[1.0, 0.0, 0.0], embed_model="f/3")
    s.mark_used([mid], "2026-02-02T00:00:00Z")
    assert s.rows_for([mid])[mid]["last_used_at"] == "2026-02-02T00:00:00Z"
    s.close()


def test_prune_removes_low_weight(tmp_path):
    s = _ws(tmp_path)
    g = s.add("g", embedding=[1.0, 0.0, 0.0], embed_model="f/3")
    b = s.add("b", embedding=[0.0, 1.0, 0.0], embed_model="f/3")
    s.rate(g, 3, 1.0)   # weight 1.5
    s.rate(b, 0, 1.0)   # weight 0.5
    assert s.prune(weight_floor=0.6) == 1
    assert s.get(b) is None and s.get(g) is not None
    s.close()
