"""Adapter that makes a libsql Connection present the sqlite3 API surface that
SessionDB depends on: settable row_factory (column-by-name access) and the
isolation_level=None + explicit-BEGIN-IMMEDIATE transaction idiom.

libsql's Connection (v0.1.11) is NOT a drop-in for sqlite3: rows are tuples with
no row_factory, isolation_level is read-only, and an explicit `BEGIN IMMEDIATE`
errors because libsql auto-opens a transaction. This adapter papers over exactly
those gaps and passes everything else straight through.
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any


@contextlib.contextmanager
def _translate():
    """Translate non-sqlite3 exceptions from libsql into sqlite3 equivalents.

    libsql raises plain ``ValueError`` for all errors (syntax errors, missing
    tables, constraint violations, lock contention, …).  SessionDB's retry loop
    catches ``sqlite3.OperationalError`` and its constraint handler catches
    ``sqlite3.IntegrityError``, so raw ``ValueError`` escapes would bypass both
    graceful-degradation paths.

    Mapping (message-based, case-insensitive):
    * "constraint failed" → sqlite3.IntegrityError  (matches all of UNIQUE /
      NOT NULL / CHECK / FOREIGN KEY "… constraint failed: …"; the exact phrase
      avoids misrouting errors that merely mention a "constraint"-named column,
      e.g. "no such column: my_constraint_col")
    * anything else       → sqlite3.OperationalError  (safe default, incl.
      "database is locked" / "busy" — what SessionDB's retry loop catches)

    Already-sqlite3 exceptions are re-raised unchanged (no double-wrapping).
    ``KeyboardInterrupt`` / ``SystemExit`` are never caught.
    """
    try:
        yield
    except sqlite3.Error:
        raise  # already the right type — don't double-wrap
    except Exception as exc:
        msg = str(exc)
        if "constraint failed" in msg.lower():
            raise sqlite3.IntegrityError(msg) from exc
        raise sqlite3.OperationalError(msg) from exc


class _Row:
    """sqlite3.Row-like: supports both row[int] and row['col'] plus keys().

    NOTE: this is intentionally NOT a subclass of sqlite3.Row (a C type that
    can't be reliably constructed/subclassed). SessionDB has defensive sites of
    the form `row["x"] if isinstance(row, sqlite3.Row) else row[0]`; for those,
    isinstance is False so they take the `row[0]` index branch — which is correct
    because those queries select the relevant value as column 0. All other sites
    do `row["col"]` directly, which this class supports.
    """

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: tuple[str, ...], vals: tuple) -> None:
        self._cols = cols
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._vals[self._cols.index(key)]
        return self._vals[key]  # int or slice

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __eq__(self, other):
        if isinstance(other, _Row):
            return self._vals == other._vals
        return self._vals == other  # allow comparison against a plain tuple

    def __repr__(self):
        return f"_Row({dict(zip(self._cols, self._vals))!r})"


def _colnames(description) -> tuple[str, ...]:
    return tuple(d[0] for d in (description or ()))


# Statements the adapter intercepts (case-insensitive, leading whitespace stripped).
def _is_begin(sql: str) -> bool:
    s = sql.lstrip().upper()
    return s.startswith("BEGIN")


def _is_commit(sql: str) -> bool:
    return sql.lstrip().upper().startswith("COMMIT")


def _is_rollback(sql: str) -> bool:
    return sql.lstrip().upper().startswith("ROLLBACK")


class _TursoCursor:
    def __init__(self, raw_cursor, row_factory):
        self._raw = raw_cursor
        self._row_factory = row_factory

    def _wrap(self, row):
        if row is None or self._row_factory is None:
            return row
        return _Row(_colnames(self._raw.description), tuple(row))

    # Cursor-level execute family. SessionDB calls these on cursors (e.g.
    # `cursor.executescript(SCHEMA_SQL)` in _init_schema, and `cursor.execute(...)`
    # widely), exactly like stdlib sqlite3 cursors. Each returns self so the
    # stdlib idiom `cursor.execute(sql).fetchone()` keeps working.
    def execute(self, sql, parameters=()):
        with _translate():
            self._raw.execute(sql, parameters)
        return self

    def executemany(self, sql, seq_of_parameters):
        with _translate():
            self._raw.executemany(sql, seq_of_parameters)
        return self

    def executescript(self, script):
        with _translate():
            self._raw.executescript(script)
        return self

    def fetchone(self):
        with _translate():
            return self._wrap(self._raw.fetchone())

    def fetchall(self):
        with _translate():
            return [self._wrap(r) for r in self._raw.fetchall()]

    def fetchmany(self, size=None):
        with _translate():
            rows = self._raw.fetchmany(size) if size is not None else self._raw.fetchmany()
            return [self._wrap(r) for r in rows]

    @property
    def description(self):
        return self._raw.description

    @property
    def lastrowid(self):
        return self._raw.lastrowid

    @property
    def rowcount(self):
        return self._raw.rowcount

    def __iter__(self):
        with _translate():
            for r in self._raw:
                yield self._wrap(r)

    def close(self):
        return self._raw.close()


class _TursoConnection:
    def __init__(self, raw_conn):
        self._raw = raw_conn
        self.row_factory = None       # SessionDB sets this to sqlite3.Row
        self._isolation_level = None  # accepted, ignored (libsql manages txns)

    # isolation_level: accept assignment as a no-op so `isolation_level=None`
    # construction and any later set doesn't blow up.
    @property
    def isolation_level(self):
        return self._isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self._isolation_level = value

    def execute(self, sql: str, parameters=()):
        # Translate SessionDB's explicit-transaction idiom to libsql semantics.
        if _is_begin(sql):
            # libsql auto-opens a transaction; an explicit BEGIN errors. No-op.
            return _TursoCursor(self._raw.cursor(), self.row_factory)
        if _is_commit(sql):
            with _translate():
                self._raw.commit()
            return _TursoCursor(self._raw.cursor(), self.row_factory)
        if _is_rollback(sql):
            with _translate():
                self._raw.rollback()
            return _TursoCursor(self._raw.cursor(), self.row_factory)
        with _translate():
            raw_cur = self._raw.execute(sql, parameters)
        return _TursoCursor(raw_cur, self.row_factory)

    def executemany(self, sql: str, seq_of_parameters: Any):
        with _translate():
            raw_cur = self._raw.executemany(sql, seq_of_parameters)
        return _TursoCursor(raw_cur, self.row_factory)

    def executescript(self, script: str):
        # libsql's executescript returns None, so we cannot wrap its return
        # value in _TursoCursor. Execute the script, then return a fresh cursor.
        with _translate():
            self._raw.executescript(script)
        return self.cursor()

    def cursor(self):
        return _TursoCursor(self._raw.cursor(), self.row_factory)

    def commit(self):
        with _translate():
            return self._raw.commit()

    def rollback(self):
        with _translate():
            return self._raw.rollback()

    def sync(self):
        return self._raw.sync()

    @property
    def in_transaction(self):
        return self._raw.in_transaction

    def close(self):
        return self._raw.close()
