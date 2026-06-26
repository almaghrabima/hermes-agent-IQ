from plugins.memory.turso_memory.store import TursoMemoryStore, new_ulid


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


def test_feedback_adjusts_trust(tmp_path):
    s = _store(tmp_path)
    mid = s.add("rate me", embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    s.feedback(mid, helpful=True)
    assert s.get(mid)["trust_score"] > 0.5
    s.feedback(mid, helpful=False)
    assert s.get(mid)["trust_score"] < 0.55
    s.close()
