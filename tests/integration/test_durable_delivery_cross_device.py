"""Cross-device durable delivery (R1): a run_id-tagged message persisted on
device A is visible to device B, and B's drain of a reconciled local row for the
same run_id is skipped (no duplicate). Requires a live Turso DB.

Marked ``integration`` (excluded by default; needs TURSO_TEST_URL/TURSO_TEST_TOKEN).
Mirrors tests/integration/test_turso_sessiondb_live.py for setup.
"""
import importlib
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
    """Create a HERMES_HOME directory with a config.yaml wired to the shared Turso DB.
    Mirrors _write_turso_home from test_turso_sessiondb_live.py exactly."""
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


def test_tagged_message_visible_cross_device_and_dedups(tmp_path, monkeypatch):
    """R1 durable delivery cross-device integration test.

    Proves three things:
    1. A message tagged with platform_message_id=run_id on device A is visible
       to device B after a sync round-trip.
    2. B's SessionDB.has_platform_message_id returns True for that run_id.
    3. drain_outbox_for_sessions with db_b skips the row (returns []) because
       the tagged message is already present in synced history.
    """
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TEST_TOKEN)
    session_id = "ddr-" + uuid.uuid4().hex[:12]
    run_id = "rlm-run-" + uuid.uuid4().hex[:12]

    # --- Device A: append a message tagged with run_id, close (flush sync push) ---
    home_a = _write_turso_home(tmp_path, "deviceA")
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    from hermes_state import SessionDB

    db_a = SessionDB(db_path=home_a / "ignored.db")
    assert type(db_a._conn).__name__ == "_TursoConnection"

    db_a.create_session(session_id=session_id, source="cli")
    db_a.append_message(
        session_id,
        role="user",
        content="durable result A",
        platform_message_id=run_id,
    )
    db_a.close()  # shutdown sync() flush pushes to the cloud primary

    # --- Device B: fresh replica, pull from cloud, assert tagged message visible ---
    home_b = _write_turso_home(tmp_path, "deviceB")
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    import hermes_state as hs

    importlib.reload(hs)
    db_b = hs.SessionDB(db_path=home_b / "ignored.db")
    db_b._conn.sync()  # pull latest from cloud primary

    assert db_b.has_platform_message_id(session_id, run_id) is True

    # --- Device B reconciles the same run into its local outbox ---
    # (simulates a temporal completion arriving at B while A already delivered it)
    from plugins.temporal import outbox
    import plugins.temporal.delivery as delivery

    outbox.record_completion(
        run_id,
        session_id,
        "completed",
        {"goal": "g", "summary": "done"},
    )

    # drain must skip the row: has_platform_message_id is True → _already_surfaced → no re-forge
    events = delivery.drain_outbox_for_sessions([session_id], db_b)
    assert events == []

    db_b.close()
