"""Tests for reap_temporal_worker — records the SQLite outcome of a
Temporal-supervised kanban worker subprocess after it exits.
"""
from hermes_cli import kanban_db


def test_reap_terminal_when_card_already_done(kanban_conn):
    task_id = kanban_db.create_task(kanban_conn, title="x", assignee="default")
    # Simulate the worker having completed the card itself.
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    assert kanban_db.reap_temporal_worker(kanban_conn, task_id, 0) == "terminal"


def test_reap_protocol_violation_on_clean_exit_still_running(kanban_conn):
    task_id = kanban_db.create_task(kanban_conn, title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET status='running', run_kind='temporal' WHERE id=?",
            (task_id,),
        )
    assert kanban_db.reap_temporal_worker(kanban_conn, task_id, 0) == "protocol_violation"


def test_reap_failed_on_nonzero_exit(kanban_conn):
    task_id = kanban_db.create_task(kanban_conn, title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET status='running', run_kind='temporal' WHERE id=?",
            (task_id,),
        )
    assert kanban_db.reap_temporal_worker(kanban_conn, task_id, 1) == "failed"


def test_reap_rate_limited_releases_without_failure(kanban_conn):
    """Exit code KANBAN_RATE_LIMIT_EXIT_CODE returns 'rate_limited' and resets
    the task back to 'ready' without incrementing the failure counter."""
    task_id = kanban_db.create_task(kanban_conn, title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET status='running', run_kind='temporal' WHERE id=?",
            (task_id,),
        )
    result = kanban_db.reap_temporal_worker(
        kanban_conn, task_id, kanban_db.KANBAN_RATE_LIMIT_EXIT_CODE
    )
    assert result == "rate_limited"
    task = kanban_db.get_task(kanban_conn, task_id)
    assert task is not None
    assert task.status == "ready"
    # Failure counter must NOT have been incremented.
    row = kanban_conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["consecutive_failures"] == 0
