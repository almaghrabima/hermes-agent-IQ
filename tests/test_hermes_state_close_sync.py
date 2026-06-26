"""Tests for SessionDB.close() Turso sync flush behaviour.

Phase 5 (turso-backend-shim): when the underlying connection exposes a .sync()
method (libsql embedded-replica), close() must call it before closing so that
a multi-device user sees the session on their other devices.  For stdlib
sqlite3 connections (no .sync attribute) the behaviour must be a no-op.
"""

from __future__ import annotations


def test_close_sqlite_no_sync_no_crash(tmp_path, monkeypatch):
    """stdlib sqlite3 backend: close() must not crash and must not call sync."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    db.append_message("s1", role="user", content="hi")
    db.close()  # must not raise; sqlite3.Connection has no .sync


def test_close_calls_sync_when_present(tmp_path, monkeypatch):
    """A connection that exposes .sync() must have it called on close()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")

    calls = {"sync": 0, "close": 0}
    real = db._conn

    class _FakeConn:
        def execute(self, *a, **k):
            return real.execute(*a, **k)

        def sync(self):
            calls["sync"] += 1

        def close(self):
            calls["close"] += 1
            real.close()

    db._conn = _FakeConn()
    db.close()
    assert calls["sync"] == 1
    assert calls["close"] == 1
