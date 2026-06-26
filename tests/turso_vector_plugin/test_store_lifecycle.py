from agent.db_backend import connect
from plugins.memory.turso_vector.store import VectorStore


def _store(tmp_path):
    conn = connect(str(tmp_path / "memory_vec.db"), label="memory_vec.db",
                   sync=None, prefer_libsql=True)
    s = VectorStore(conn, dim=4)
    s.migrate()
    return s


def _insert(s, text, weight=1.0, created="2026-06-27T00:00:00+00:00"):
    return s.insert(kind="insight", project="a", cwd="/a", text=text,
                    what_failed=None, what_worked=None, embedding=[1, 0, 0, 0],
                    created_at=created, source_session="s1", weight=weight)


def test_apply_rating_promotes_top_score(tmp_path):
    s = _store(tmp_path)
    mid = _insert(s, "x", weight=1.0)
    s.apply_rating(mid, score=3, alpha=0.4)
    row = s._conn.execute("SELECT weight, ema_rating FROM memories WHERE id=?", (mid,)).fetchone()
    assert row[0] > 1.0          # promoted
    assert row[1] == 1.0         # first rating ema == normalized score


def test_apply_rating_demotes_zero_score(tmp_path):
    s = _store(tmp_path)
    mid = _insert(s, "x", weight=1.0)
    s.apply_rating(mid, score=0, alpha=0.4)
    weight = s._conn.execute("SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()[0]
    assert weight < 1.0          # demoted


def test_decay_and_prune_removes_sub_floor(tmp_path):
    s = _store(tmp_path)
    keep = _insert(s, "keep", weight=1.0, created="2026-06-27T00:00:00+00:00")
    drop = _insert(s, "drop", weight=0.2, created="2026-01-01T00:00:00+00:00")
    pruned = s.decay_and_prune(ids=[keep, drop], now="2026-06-27T00:00:00+00:00",
                               decay_rate=0.9, weight_floor=0.15)
    remaining = {r[0] for r in s._conn.execute("SELECT text FROM memories").fetchall()}
    assert pruned == 1
    assert "keep" in remaining and "drop" not in remaining


def test_delete(tmp_path):
    s = _store(tmp_path)
    mid = _insert(s, "x")
    assert s.delete(mid) is True
    assert s.count() == 0
    assert s.delete(mid) is False


def test_mark_used_increments(tmp_path):
    s = _store(tmp_path)
    mid = _insert(s, "x")
    s.mark_used([mid], now="2026-06-27T12:00:00+00:00")
    row = s._conn.execute("SELECT use_count, last_used_at FROM memories WHERE id=?", (mid,)).fetchone()
    assert row[0] == 1 and row[1] == "2026-06-27T12:00:00+00:00"
