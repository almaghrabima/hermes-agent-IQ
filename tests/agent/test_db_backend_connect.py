import sqlite3

from agent.db_backend import connect


def test_connect_sqlite_passthrough_returns_real_sqlite(tmp_path):
    db = tmp_path / "x.db"
    conn = connect(str(db), label="x.db")
    assert isinstance(conn, sqlite3.Connection)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    assert conn.execute("SELECT id FROM t").fetchone()[0] == 7
    conn.close()


def test_connect_forwards_sqlite_kwargs(tmp_path):
    db = tmp_path / "y.db"
    # isolation_level=None + check_same_thread mirror SessionDB's usage
    conn = connect(
        str(db), label="y.db", isolation_level=None, check_same_thread=False, timeout=1.0
    )
    assert conn.isolation_level is None
    conn.close()


def test_connect_prefer_libsql_local_has_native_vectors(tmp_path):
    """prefer_libsql opens a local libSQL connection (no sync) so native vector
    SQL (F32_BLOB / vector32 / vector_distance_cos) works — stdlib sqlite3 can't."""
    import pytest
    pytest.importorskip("libsql")
    from agent.db_backend import connect

    conn = connect(str(tmp_path / "v.db"), label="memory.db", sync=None, prefer_libsql=True)
    conn.execute("CREATE TABLE m (id TEXT PRIMARY KEY, e F32_BLOB(3))")
    conn.execute("INSERT INTO m (id, e) VALUES ('a', vector32('[1.0,0.0,0.0]'))")
    conn.execute("INSERT INTO m (id, e) VALUES ('b', vector32('[0.0,0.0,1.0]'))")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM m ORDER BY vector_distance_cos(e, vector32('[0.9,0.1,0.0]')) ASC"
    ).fetchall()]
    assert ids[0] == "a"
    conn.close()


def test_connect_sync_none_default_is_still_stdlib_sqlite(tmp_path):
    import sqlite3
    from agent.db_backend import connect
    conn = connect(str(tmp_path / "s.db"), label="x", sync=None)  # no prefer_libsql
    assert isinstance(conn, sqlite3.Connection)
    conn.close()
