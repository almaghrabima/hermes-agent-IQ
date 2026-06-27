from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from hermes_constants import get_hermes_home

_lock = threading.Lock()

def _db_path() -> Path:
    return get_hermes_home() / "temporal_outbox.db"

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), isolation_level=None, check_same_thread=False)
    # The worker process writes while the Hermes process drains (BEGIN IMMEDIATE)
    # against the same file. Wait on lock contention instead of failing immediately
    # with "database is locked" — explicit so the guarantee doesn't ride on CPython's
    # sqlite3 connect(timeout=5.0) default.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        " run_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, status TEXT NOT NULL,"
        " block TEXT NOT NULL, created_at REAL NOT NULL, delivered_at REAL)"
    )
    return conn

def record_completion(run_id: str, session_key: str, status: str, block: dict) -> None:
    """Persist a completed durable delegation. Idempotent on run_id (first write wins)."""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO outbox(run_id, session_key, status, block, created_at)"
                " VALUES(?,?,?,?,?)",
                (run_id, session_key or "default", status, json.dumps(block), time.time()),
            )
        finally:
            conn.close()

def has_run(run_id: str) -> bool:
    with _lock:
        conn = _conn()
        try:
            return conn.execute("SELECT 1 FROM outbox WHERE run_id=?", (run_id,)).fetchone() is not None
        finally:
            conn.close()

def claim_undelivered(session_keys: list[str], limit: int = 50) -> list[dict[str, Any]]:
    """Atomically fetch + mark-delivered undelivered rows for the given session keys.

    Deliberately at-most-once: a row is marked delivered at claim time, so a crash
    between this call and the result reaching the conversation drops that result
    (reconcile won't recover it — has_run is already true). The larger durability
    gap (a delegation completing while no Hermes process is alive) is covered by
    reconcile + record_completion idempotency; only the sub-second claim->consume
    window is exposed. At-least-once would need an ack-after-consume across the
    CLI/gateway/TUI consumers and risks duplicate delivery — intentionally not done.
    """
    if not session_keys:
        return []
    with _lock:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            qs = ",".join("?" for _ in session_keys)
            rows = conn.execute(
                f"SELECT run_id, session_key, status, block FROM outbox"
                f" WHERE delivered_at IS NULL AND session_key IN ({qs})"
                f" ORDER BY created_at LIMIT ?",
                (*session_keys, limit),
            ).fetchall()
            now = time.time()
            for r in rows:
                conn.execute("UPDATE outbox SET delivered_at=? WHERE run_id=?", (now, r[0]))
            conn.execute("COMMIT")
            return [
                {"run_id": r[0], "session_key": r[1], "status": r[2], "block": json.loads(r[3])}
                for r in rows
            ]
        finally:
            conn.close()

def get_row(run_id: str) -> dict | None:
    with _lock:
        conn = _conn()
        try:
            r = conn.execute(
                "SELECT run_id, session_key, status, block, delivered_at FROM outbox WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
    if r is None:
        return None
    return {"run_id": r[0], "session_key": r[1], "status": r[2],
            "block": json.loads(r[3]), "delivered_at": r[4]}
