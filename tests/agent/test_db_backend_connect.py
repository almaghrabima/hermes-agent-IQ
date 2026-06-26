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


def test_prefer_libsql_opens_local_vector_capable_connection(tmp_path):
    """sync=None + prefer_libsql=True -> libSQL conn that supports native vectors."""
    from agent.db_backend import connect
    conn = connect(str(tmp_path / "vec.db"), label="vec.db", sync=None, prefer_libsql=True)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, emb F32_BLOB(3))")
    conn.execute("INSERT INTO t(id, emb) VALUES (1, vector32('[1,2,3]'))")
    row = conn.execute(
        "SELECT vector_distance_cos(emb, vector32('[1,2,3]')) FROM t"
    ).fetchone()
    assert abs(float(row[0])) < 1e-6   # identical vectors -> ~0 cosine distance


def test_default_connect_still_returns_stdlib_sqlite(tmp_path):
    """No regression: sync=None without prefer_libsql is unchanged stdlib sqlite3."""
    import sqlite3
    from agent.db_backend import connect
    conn = connect(str(tmp_path / "plain.db"), label="plain.db")
    assert isinstance(conn, sqlite3.Connection)
