"""Real libSQL vector search: nearest vector ranks first; project boost reorders."""
from agent.db_backend import connect
from plugins.memory.turso_vector.store import VectorStore


def _store(tmp_path):
    conn = connect(str(tmp_path / "memory_vec.db"), label="memory_vec.db",
                   sync=None, prefer_libsql=True)
    s = VectorStore(conn, dim=4)
    s.migrate()
    return s


def test_nearest_vector_ranks_first(tmp_path):
    s = _store(tmp_path)
    s.insert(kind="insight", project="a", cwd="/a", text="close",
             what_failed=None, what_worked=None, embedding=[1, 0, 0, 0],
             created_at="2026-06-27T00:00:00+00:00", source_session="s1")
    s.insert(kind="insight", project="a", cwd="/a", text="far",
             what_failed=None, what_worked=None, embedding=[0, 0, 0, 1],
             created_at="2026-06-27T00:00:00+00:00", source_session="s1")
    results = s.search(query_embedding=[1, 0, 0, 0], project="a",
                       candidate_pool=10, top_k=2, beta=0.2, project_boost=0.1)
    assert results[0]["text"] == "close"
    assert results[0]["dist"] <= results[1]["dist"]


def test_project_boost_reorders_near_ties(tmp_path):
    s = _store(tmp_path)
    # Two near-equal vectors; only the project differs.
    s.insert(kind="insight", project="other", cwd="/o", text="other-proj",
             what_failed=None, what_worked=None, embedding=[1, 0, 0, 0],
             created_at="2026-06-27T00:00:00+00:00", source_session="s1")
    s.insert(kind="insight", project="current", cwd="/c", text="current-proj",
             what_failed=None, what_worked=None, embedding=[0.99, 0.01, 0, 0],
             created_at="2026-06-27T00:00:00+00:00", source_session="s1")
    results = s.search(query_embedding=[1, 0, 0, 0], project="current",
                       candidate_pool=10, top_k=2, beta=0.2, project_boost=0.5)
    # Big project_boost lifts the slightly-farther current-project memory to #1.
    assert results[0]["text"] == "current-proj"


def test_count(tmp_path):
    s = _store(tmp_path)
    assert s.count() == 0
    s.insert(kind="user", project=None, cwd=None, text="x",
             what_failed=None, what_worked=None, embedding=[1, 1, 1, 1],
             created_at="2026-06-27T00:00:00+00:00", source_session="s1")
    assert s.count() == 1
