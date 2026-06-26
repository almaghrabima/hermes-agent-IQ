"""Adapter that makes a libsql Connection present the sqlite3 API surface that
SessionDB depends on: settable row_factory (column-by-name access) and the
isolation_level=None + explicit-BEGIN-IMMEDIATE transaction idiom.

libsql's Connection (v0.1.11) is NOT a drop-in for sqlite3: rows are tuples with
no row_factory, isolation_level is read-only, and an explicit `BEGIN IMMEDIATE`
errors because libsql auto-opens a transaction. This adapter papers over exactly
those gaps and passes everything else straight through.
"""
from __future__ import annotations

from typing import Any


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

    def fetchone(self):
        return self._wrap(self._raw.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._raw.fetchall()]

    def fetchmany(self, size=None):
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
            self._raw.commit()
            return _TursoCursor(self._raw.cursor(), self.row_factory)
        if _is_rollback(sql):
            self._raw.rollback()
            return _TursoCursor(self._raw.cursor(), self.row_factory)
        raw_cur = self._raw.execute(sql, parameters)
        return _TursoCursor(raw_cur, self.row_factory)

    def executemany(self, sql: str, seq_of_parameters: Any):
        return _TursoCursor(self._raw.executemany(sql, seq_of_parameters), self.row_factory)

    def executescript(self, script: str):
        return _TursoCursor(self._raw.executescript(script), self.row_factory)

    def cursor(self):
        return _TursoCursor(self._raw.cursor(), self.row_factory)

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def sync(self):
        return self._raw.sync()

    @property
    def in_transaction(self):
        return self._raw.in_transaction

    def close(self):
        return self._raw.close()
