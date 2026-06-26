"""Live integration: real SessionDB through the Turso/libsql adapter, including
a cross-device sync round-trip.

Marked ``integration`` (excluded from the default suite) and skipped unless a
Turso dev database is provided via env:

    TURSO_TEST_URL=libsql://...  TURSO_TEST_TOKEN=...  \
        scripts/run_tests.sh -m integration tests/integration/test_turso_sessiondb_live.py

Device A and Device B are two separate local replicas (two HERMES_HOMEs) both
syncing to the same cloud DB. The test exercises the real SessionDB code paths
(schema init, row_factory column access, transaction translation, FTS5 triggers
+ search, shutdown sync flush) against libsql, then proves a write on A is
visible on B after sync.
"""
import os
import textwrap
import uuid
from pathlib import Path

import pytest

pytest.importorskip("libsql")

TEST_URL = os.environ.get("TURSO_TEST_URL")
TEST_TOKEN = os.environ.get("TURSO_TEST_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (TEST_URL and TEST_TOKEN),
        reason="TURSO_TEST_URL / TURSO_TEST_TOKEN not set",
    ),
]


def _write_turso_home(tmp_path: Path, tag: str) -> Path:
    home = tmp_path / tag
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(f"""
            database:
              backend: turso
              turso:
                sync_url: "{TEST_URL}"
                sync_interval: 5
                local_path: "{home / 'replica.db'}"
        """),
        encoding="utf-8",
    )
    return home


def test_sessiondb_on_turso_cross_device(tmp_path, monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TEST_TOKEN)
    sid = "it-" + uuid.uuid4().hex[:12]
    contents = [
        f"hello from device A {sid}",
        f"the eagle {sid} lands at midnight",
    ]

    # --- Device A: write through real SessionDB on Turso ---
    home_a = _write_turso_home(tmp_path, "deviceA")
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    from hermes_state import SessionDB

    db_a = SessionDB(db_path=home_a / "ignored.db")
    # The connection is the libsql adapter, not stdlib sqlite3.
    assert type(db_a._conn).__name__ == "_TursoConnection"

    db_a.create_session(session_id=sid, source="cli")
    for c in contents:
        db_a.append_message(sid, role="user", content=c)

    # row_factory column-by-name access through real SessionDB.
    msgs_a = db_a.get_messages(sid)
    assert [m["content"] for m in msgs_a] == contents

    # FTS5 triggers populated + MATCH works through the adapter.
    hits = db_a.search_messages(f"eagle {sid}")
    assert len(hits) >= 1

    db_a.close()  # shutdown sync() flush pushes to the cloud primary

    # --- Device B: a fresh replica must see A's session after sync ---
    home_b = _write_turso_home(tmp_path, "deviceB")
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    import importlib
    import hermes_state as hs

    importlib.reload(hs)
    db_b = hs.SessionDB(db_path=home_b / "ignored.db")
    db_b._conn.sync()  # pull latest from cloud

    msgs_b = db_b.get_messages(sid)
    assert [m["content"] for m in msgs_b] == contents
    db_b.close()
