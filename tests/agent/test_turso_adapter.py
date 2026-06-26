"""Offline tests for the _TursoConnection sqlite3-compat adapter.

All tests use a LOCAL libsql connection (no sync_url, no network, no creds).
The module is skipped entirely if libsql is not importable.
"""
from __future__ import annotations

import sqlite3

import pytest

libsql = pytest.importorskip("libsql")

from agent.turso_adapter import _TursoConnection  # noqa: E402


def _conn(tmp_path):
    """Open a local libsql connection wrapped in _TursoConnection."""
    raw = libsql.connect(str(tmp_path / "local.db"))
    return _TursoConnection(raw)


# ---------------------------------------------------------------------------
# Test 1: row_factory — name + index access
# ---------------------------------------------------------------------------


def test_row_factory_name_and_index_access(tmp_path):
    conn = _conn(tmp_path)
    conn.row_factory = sqlite3.Row  # SessionDB sets this

    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t VALUES (?, ?)", (1, "alice"))
    conn.commit()

    row = conn.execute("SELECT id, name FROM t").fetchone()
    assert row["name"] == "alice"
    assert row["id"] == 1
    assert row[0] == 1
    assert row[1] == "alice"
    conn.close()


# ---------------------------------------------------------------------------
# Test 2: row_factory None → plain tuple
# ---------------------------------------------------------------------------


def test_row_factory_none_returns_tuple(tmp_path):
    conn = _conn(tmp_path)
    # row_factory stays None (default)

    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (?)", (42,))
    conn.commit()

    row = conn.execute("SELECT id FROM t").fetchone()
    assert isinstance(row, tuple)
    assert row[0] == 42
    conn.close()


# ---------------------------------------------------------------------------
# Test 3: BEGIN/COMMIT translation — inserts persist
# ---------------------------------------------------------------------------


def test_begin_commit_translation_persists(tmp_path):
    conn = _conn(tmp_path)

    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()

    # SessionDB's explicit-transaction idiom
    conn.execute("BEGIN IMMEDIATE")  # must not raise
    conn.execute("INSERT INTO t VALUES (?)", (7,))
    conn.execute("COMMIT")

    # Re-query to confirm persistence
    row = conn.execute("SELECT id FROM t").fetchone()
    assert row[0] == 7
    conn.close()


# ---------------------------------------------------------------------------
# Test 4: ROLLBACK translation — inserts discarded
# ---------------------------------------------------------------------------


def test_rollback_translation_discards(tmp_path):
    conn = _conn(tmp_path)

    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t VALUES (?)", (99,))
    conn.execute("ROLLBACK")

    rows = conn.execute("SELECT id FROM t").fetchall()
    assert rows == []
    conn.close()


# ---------------------------------------------------------------------------
# Test 5: executescript — multi-statement DDL + DML
# ---------------------------------------------------------------------------


def test_executescript_multistatement(tmp_path):
    conn = _conn(tmp_path)

    conn.executescript("""
        CREATE TABLE a (x INTEGER);
        CREATE TABLE b (y TEXT);
        INSERT INTO a VALUES (1);
        INSERT INTO b VALUES ('hello');
    """)

    assert conn.execute("SELECT x FROM a").fetchone()[0] == 1
    assert conn.execute("SELECT y FROM b").fetchone()[0] == "hello"
    conn.close()


# ---------------------------------------------------------------------------
# Test 6: FTS5 + trigram tokenizer
# ---------------------------------------------------------------------------


def test_fts5_trigram_search(tmp_path):
    conn = _conn(tmp_path)

    conn.executescript("""
        CREATE VIRTUAL TABLE docs USING fts5(body, tokenize='trigram');
        INSERT INTO docs VALUES ('the quick brown fox');
        INSERT INTO docs VALUES ('a lazy dog');
    """)

    # Trigram tokenizer supports substring match
    rows = conn.execute("SELECT body FROM docs WHERE docs MATCH 'rown'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "the quick brown fox"
    conn.close()


# ---------------------------------------------------------------------------
# Test 7: foreign_keys cascade
# ---------------------------------------------------------------------------


def test_foreign_keys_cascade(tmp_path):
    conn = _conn(tmp_path)

    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY);
        CREATE TABLE child (id INTEGER, parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE);
        INSERT INTO parent VALUES (1);
        INSERT INTO child VALUES (10, 1);
    """)
    conn.commit()

    conn.execute("DELETE FROM parent WHERE id = ?", (1,))
    conn.commit()

    rows = conn.execute("SELECT id FROM child").fetchall()
    assert rows == []
    conn.close()


# ---------------------------------------------------------------------------
# Test 8: _execute_write-shape round-trip (mimics hermes_state.py ~line 952)
# ---------------------------------------------------------------------------


def test_execute_write_shape_round_trip(tmp_path):
    """Mimic SessionDB._execute_write: BEGIN IMMEDIATE + INSERT with params + commit()."""
    conn = _conn(tmp_path)
    conn.row_factory = sqlite3.Row

    conn.execute("CREATE TABLE sessions (id TEXT, data TEXT)")
    conn.commit()

    # The exact idiom from hermes_state.py
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO sessions VALUES (?, ?)", ("sess-1", "payload"))
    conn.commit()

    row = conn.execute("SELECT id, data FROM sessions WHERE id = ?", ("sess-1",)).fetchone()
    assert row["id"] == "sess-1"
    assert row["data"] == "payload"
    conn.close()


# ---------------------------------------------------------------------------
# Test 9: exception translation — bad SQL raises sqlite3.OperationalError
# (Finding 1: libsql raises ValueError; adapter must translate to sqlite3)
# ---------------------------------------------------------------------------


def test_bad_sql_raises_operational_error(tmp_path):
    """Bad SQL through the adapter must surface as sqlite3.OperationalError.

    libsql raises ValueError for all errors; SessionDB's _execute_write retry
    loop catches sqlite3.OperationalError, so the translation is load-bearing.
    """
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM")  # syntax error
    conn.close()


# ---------------------------------------------------------------------------
# Test 10: exception translation — UNIQUE constraint raises sqlite3.IntegrityError
# (Finding 1: constraint violations must map to IntegrityError)
# ---------------------------------------------------------------------------


def test_unique_constraint_raises_integrity_error(tmp_path):
    """Duplicate primary key must surface as sqlite3.IntegrityError.

    libsql raises ValueError("UNIQUE constraint failed: …"); the adapter must
    detect the word 'constraint' in the message and raise sqlite3.IntegrityError
    so SessionDB's constraint-handling graceful-degradation path fires.
    """
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE uk (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO uk VALUES (?)", (1,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO uk VALUES (?)", (1,))  # duplicate
    conn.close()


# ---------------------------------------------------------------------------
# Test 11: executescript returns a usable cursor (Finding 2)
# ---------------------------------------------------------------------------


def test_executescript_returns_usable_cursor(tmp_path):
    """executescript must return a cursor on which fetchone()/iteration are safe.

    libsql's executescript() returns None; wrapping None in _TursoCursor then
    calling .fetchone() raises AttributeError. The adapter must return a fresh,
    usable cursor instead.
    """
    conn = _conn(tmp_path)
    cur = conn.executescript("CREATE TABLE a(x); CREATE TABLE b(y);")
    # Must not raise AttributeError
    result = cur.fetchone()
    assert result is None  # empty result set is fine; just must not crash
    conn.close()
