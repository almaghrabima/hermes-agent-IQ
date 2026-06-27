import importlib
import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_state
    importlib.reload(hermes_state)
    d = hermes_state.SessionDB(db_path=str(tmp_path / "state.db"))
    yield d
    d.close()


def test_message_id_is_snowflake_and_has_origin(db):
    db.create_session("s1", source="cli")
    mid = db.append_message("s1", role="user", content="hello")
    # Snowflake ids are huge (time-prefixed), never small autoincrement values
    assert mid > (1 << 40)
    row = db._conn.execute(
        "SELECT origin_device FROM messages WHERE id=?", (mid,)
    ).fetchone()
    assert row[0]  # origin_device populated (non-empty)


def test_messages_keep_chronological_order(db):
    db.create_session("s1", source="cli")
    ids = [db.append_message("s1", role="user", content=f"m{i}") for i in range(5)]
    assert ids == sorted(ids)  # monotonic
    fetched = db._conn.execute(
        "SELECT id FROM messages WHERE session_id='s1' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in fetched] == ids


def test_fts_still_searchable_after_snowflake_ids(db):
    db.create_session("s1", source="cli")
    db.append_message("s1", role="user", content="pineapple supremacy")
    hits = db._conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'pineapple'"
    ).fetchall()
    assert len(hits) == 1


def test_old_autoincrement_rows_preserved_and_ordered(db):
    # Simulate a pre-hardening row: a small autoincrement-style id with NULL origin.
    db.create_session("s1", source="cli")
    conn = db._conn
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, origin_device) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (5, "s1", "user", "legacy ananas row", 1000.0),
    )
    # New snowflake-id message after the legacy one
    new_id = db.append_message("s1", role="user", content="modern row")
    # Legacy row survives, ordering puts it first (older), origin stays NULL
    rows = conn.execute(
        "SELECT id, origin_device FROM messages WHERE session_id='s1' ORDER BY id"
    ).fetchall()
    ids = [r[0] for r in rows]
    assert ids == [5, new_id] and new_id > (1 << 40)
    assert rows[0][1] is None          # legacy row: NULL origin_device
    # FTS still finds the legacy row by content
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'ananas'"
    ).fetchall()
    assert [h[0] for h in hits] == [5]


def test_two_devices_no_id_collision(tmp_path, monkeypatch):
    # Simulate two devices writing to the SAME db with different device numbers.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_state, agent.device_identity as di
    importlib.reload(hermes_state)
    shared = str(tmp_path / "shared.db")

    d1 = hermes_state.SessionDB(db_path=shared)
    d1.create_session("s1", source="cli")
    # Device A
    di._reset_cache()
    monkeypatch.setattr(di, "get_device_number", lambda: 11)
    di._process_gen = di.SnowflakeGenerator(11)
    a_ids = [d1.append_message("s1", role="user", content=f"A{i}") for i in range(20)]
    # Device B (different number, same shared db)
    monkeypatch.setattr(di, "get_device_number", lambda: 22)
    di._process_gen = di.SnowflakeGenerator(22)
    b_ids = [d1.append_message("s1", role="user", content=f"B{i}") for i in range(20)]
    assert len(set(a_ids) | set(b_ids)) == 40  # zero collisions
    d1.close()
