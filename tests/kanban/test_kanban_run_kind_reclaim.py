"""Tests that TTL/heartbeat reclaimers skip Temporal-supervised runs.

Design: two reclaimers are PID-gated (detect_crashed_workers, max-runtime
enforcer) and already skip Temporal runs because those keep worker_pid=NULL.
The OTHER two (release_stale_claims by TTL, detect_stale_running by heartbeat)
have no PID filter, so they WOULD reclaim a Temporal run on a worker-host crash
→ double-execution with Temporal's own re-run.  run_kind='temporal' is the
guard that prevents that.
"""
import time
from hermes_cli import kanban_db


def _running_task(conn, *, run_kind=None, claim_age=10_000):
    """Insert a 'running' task with an expired claim and no live pid."""
    now = int(time.time())
    # create_task returns the generated task id; use that as tid.
    tid = kanban_db.create_task(conn, title="x", assignee="default")
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, "
            "claim_expires=?, worker_pid=NULL, run_kind=? WHERE id=?",
            (f"{kanban_db._claimer_id().split(':',1)[0]}:lock", now - claim_age, run_kind, tid),
        )
    return tid


def test_release_stale_claims_skips_temporal_runs(kanban_conn):
    tid = _running_task(kanban_conn, run_kind="temporal")
    reclaimed = kanban_db.release_stale_claims(kanban_conn)
    row = kanban_conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert reclaimed == 0
    assert row["status"] == "running"   # NOT reclaimed


def test_release_stale_claims_still_reclaims_builtin(kanban_conn):
    tid = _running_task(kanban_conn, run_kind=None)
    kanban_db.release_stale_claims(kanban_conn)
    row = kanban_conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "ready"     # builtin run with dead pid → reclaimed
