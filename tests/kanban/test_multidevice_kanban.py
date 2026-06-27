"""Multi-device kanban tests: Snowflake ids + origin_device on child tables."""
import pytest
import agent.device_identity as di


def test_comment_id_is_snowflake_with_origin(kanban_conn):
    from hermes_cli import kanban_db
    tid = kanban_db.create_task(kanban_conn, title="t", assignee="default")
    cid = kanban_db.add_comment(kanban_conn, tid, "me", "hi")
    # Snowflake ids dwarf old autoincrement values
    assert int(cid) > (1 << 40)
    row = kanban_conn.execute(
        "SELECT origin_device FROM task_comments WHERE id=?", (cid,)
    ).fetchone()
    assert row["origin_device"]


def test_event_row_is_snowflake(kanban_conn):
    from hermes_cli import kanban_db
    tid = kanban_db.create_task(kanban_conn, title="t", assignee="default")
    # create_task records a 'created' event via _append_event
    ev_id = kanban_conn.execute(
        "SELECT id FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()["id"]
    assert int(ev_id) > (1 << 40)


def test_two_devices_no_comment_id_collision(kanban_conn, monkeypatch):
    from hermes_cli import kanban_db
    tid = kanban_db.create_task(kanban_conn, title="t", assignee="default")
    monkeypatch.setattr(di, "get_device_number", lambda: 31)
    di._process_gen = di.SnowflakeGenerator(31)
    a = [kanban_db.add_comment(kanban_conn, tid, "me", f"A{i}") for i in range(10)]
    monkeypatch.setattr(di, "get_device_number", lambda: 32)
    di._process_gen = di.SnowflakeGenerator(32)
    b = [kanban_db.add_comment(kanban_conn, tid, "me", f"B{i}") for i in range(10)]
    assert len({int(x) for x in a} | {int(x) for x in b}) == 20
