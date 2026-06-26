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
